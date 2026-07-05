"""Consolidated site policy scanner -- evaluates and enforces both visibility and sharing policies.

Three-phase flow:
1. Enumerate all SharePoint sites (Graph API for groups + SPO CSOM for site properties)
2. Evaluate & remediate visibility (Public -> Private for non-allowlisted groups)
3. Evaluate & remediate sharing (disable anonymous sharing for non-allowlisted sites)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import asyncpg

from .allowlist_enforcer import (
    SHARING_CAPABILITY_MAP,
    SHARING_CAPABILITY_REVERSE,
    SiteRecord,
    _capability_allows_anonymous,
    _create_spo_admin_context,
    _encode_csom_url,
    _get_sharing_capability_by_url_sync,
    _is_unsupported_site_type,
    _set_sharing_capability_sync,
    get_site_inventory,
)
from .config import LifecycleConfig
from .graph_api import (
    GraphAuth,
    batch_get_group_sites,
    enumerate_m365_groups,
    set_group_visibility,
)

logger = logging.getLogger(__name__)


def _sanitize_error(e: Exception, max_length: int = 500) -> str:
    """Truncate and sanitize exception message for storage."""
    msg = str(e)
    if "Bearer " in msg:
        msg = re.sub(r'Bearer [A-Za-z0-9\-._~+/]+=*', 'Bearer [REDACTED]', msg)
    return msg[:max_length]


class EnumerationFloorError(RuntimeError):
    """Raised when the site inventory is drastically smaller than last time."""


async def _check_enumeration_floor(
    db_pool: asyncpg.Pool,
    trigger_type: str,
    enumerated: int,
    config: LifecycleConfig,
) -> None:
    """Fail the scan if the inventory shrank past the configured floor.

    A truncated enumeration must never read as a clean 'completed' scan: the
    pre-2026-07 CSOM path silently returned only its first page (1,423 of
    ~235K sites) and every scan looked successful with 0 violations.
    Compares against the largest total from the last few completed scans of
    the same trigger_type; skipped when there is no history yet.
    """
    floor_pct = max(0, min(100, config.site_policy_scan_floor_pct))
    if floor_pct == 0:
        return
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT MAX(total_sites_scanned) AS peak
            FROM (
                SELECT total_sites_scanned
                FROM site_policy_scans
                WHERE trigger_type = $1 AND status = 'completed'
                  AND total_sites_scanned > 0
                ORDER BY completed_at DESC
                LIMIT 5
            ) recent
            """,
            trigger_type,
        )
    peak = row["peak"] if row and row["peak"] else 0
    if peak and enumerated < peak * floor_pct / 100:
        raise EnumerationFloorError(
            f"Enumeration returned {enumerated} sites but the recent peak for "
            f"'{trigger_type}' scans is {peak} (floor {floor_pct}%). Refusing to "
            "treat a truncated inventory as a completed scan."
        )


async def _enforce_sharing_on_sites(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
    ctx,
    *,
    scan_id: int,
    sites: list[SiteRecord],
    group_by_site: dict[str, dict],
    stats: dict,
    log_prefix: str,
) -> None:
    """Evaluate + remediate the anonymous-sharing policy for a site list.

    Shared by the daily SharePoint scan (Phase 3) and the weekly OneDrive
    sweep. Mutates *stats* in place and records site_policy_events rows.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT site_url FROM site_allowlist")
    allowed_sharing_urls = {r["site_url"].rstrip("/").lower() for r in rows}

    enabled_capability = SHARING_CAPABILITY_MAP.get(
        config.site_policy_enabled_sharing_capability,
        SHARING_CAPABILITY_MAP["ExternalUserAndGuestSharing"],
    )
    disabled_capability = SHARING_CAPABILITY_MAP.get(
        config.site_policy_disabled_sharing_capability,
        SHARING_CAPABILITY_MAP["ExternalUserSharingOnly"],
    )

    logger.info(
        "%s: Checking sharing (%d sites, %d allowed)",
        log_prefix, len(sites), len(allowed_sharing_urls),
    )

    capability_resolved_per_site = 0
    for record in sites:
        site_url = record.url.rstrip("/").lower() if record.url else ""
        if not site_url:
            continue
        site_display_name = record.title
        current_capability = record.sharing_capability
        if current_capability not in SHARING_CAPABILITY_REVERSE:
            # Inventory returned an uninitialized/unknown value. Treating it
            # as compliant would silently skip enforcement, so resolve the
            # authoritative value with a per-site call.
            try:
                current_capability = await asyncio.to_thread(
                    _get_sharing_capability_by_url_sync, ctx, site_url
                )
                capability_resolved_per_site += 1
                if capability_resolved_per_site % 100 == 0:
                    logger.info(
                        "%s: %d per-site capability resolutions so far",
                        log_prefix, capability_resolved_per_site,
                    )
            except Exception as e:
                stats["errors"] += 1
                logger.warning(
                    "Could not resolve SharingCapability for %s: %s", site_url, e,
                )
                continue
            if current_capability not in SHARING_CAPABILITY_REVERSE:
                stats["errors"] += 1
                logger.warning(
                    "SharingCapability still unknown for %s (value=%r) — skipping",
                    site_url, current_capability,
                )
                continue
        current_name = SHARING_CAPABILITY_REVERSE.get(
            current_capability, str(current_capability)
        )
        is_allowed = site_url in allowed_sharing_urls

        # Get group_id if this is a group-connected site
        group_info = group_by_site.get(site_url, {})
        gid = group_info.get("group_id", "")

        if not is_allowed and _capability_allows_anonymous(current_capability):
            # Violation: anonymous sharing on non-allowed site
            stats["sharing_violations_found"] += 1
            desired_name = SHARING_CAPABILITY_REVERSE.get(
                disabled_capability, str(disabled_capability)
            )
            action = "remediated"
            error_msg = None

            try:
                await asyncio.to_thread(
                    _set_sharing_capability_sync, ctx, site_url, disabled_capability
                )
                stats["sharing_remediated"] += 1
            except Exception as e:
                action = "failed"
                error_msg = _sanitize_error(e)
                stats["errors"] += 1
                logger.error("Failed to disable sharing on %s: %s", site_url, e)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO site_policy_events
                        (scan_id, policy_type, site_url, site_display_name,
                         group_id, previous_value, new_value, action, error_message)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    scan_id, "sharing", site_url, site_display_name,
                    gid, current_name, desired_name, action, error_msg,
                )

            await asyncio.sleep(0.2)  # Rate limit

        elif is_allowed and current_capability != enabled_capability:
            # Allowed site with wrong capability -- enable it
            desired_name = SHARING_CAPABILITY_REVERSE.get(
                enabled_capability, str(enabled_capability)
            )
            action = "remediated"
            error_msg = None

            try:
                await asyncio.to_thread(
                    _set_sharing_capability_sync, ctx, site_url, enabled_capability
                )
                stats["sharing_remediated"] += 1
            except Exception as e:
                action = "failed"
                error_msg = _sanitize_error(e)
                stats["errors"] += 1
                logger.error("Failed to enable sharing on %s: %s", site_url, e)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO site_policy_events
                        (scan_id, policy_type, site_url, site_display_name,
                         group_id, previous_value, new_value, action, error_message)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    scan_id, "sharing", site_url, site_display_name,
                    gid, current_name, desired_name, action, error_msg,
                )

            await asyncio.sleep(0.2)

    if capability_resolved_per_site:
        logger.info(
            "%s: resolved %d sites' SharingCapability per-site "
            "(inventory value was uninitialized)",
            log_prefix, capability_resolved_per_site,
        )


async def cascade_policies_on_private_flip(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
    ctx,
    *,
    scan_id: int,
    site_url: str,
    site_display_name: str,
    group_id: str,
) -> None:
    """Apply the sharing-capability + CWS policies on a site immediately after
    its visibility was flipped Public -> Private.

    Two cascades:
      1. If anonymous sharing is currently enabled and the site is not on the
         sharing allow list, disable it (same CSOM call as Phase 3).
      2. Record a CWS drift event so the daily PowerShell drift remediation
         disables DisableCompanyWideSharingLinks on this site. Skipped if the
         site is on the CWS allow list.

    Best-effort: errors are logged + recorded but never raised — a successful
    visibility flip should not be rolled back by a failed cascade.
    """
    norm_url = site_url.rstrip("/").lower() if site_url else ""
    if not norm_url:
        return

    # --- Cascade 1: anonymous sharing (CSOM, per-site) ----------------------
    try:
        async with db_pool.acquire() as conn:
            allowed_row = await conn.fetchrow(
                "SELECT 1 FROM site_allowlist WHERE LOWER(RTRIM(site_url, '/')) = $1 LIMIT 1",
                norm_url,
            )
        if not allowed_row:
            disabled_capability = SHARING_CAPABILITY_MAP.get(
                config.site_policy_disabled_sharing_capability,
                SHARING_CAPABILITY_MAP["ExternalUserSharingOnly"],
            )
            desired_name = SHARING_CAPABILITY_REVERSE.get(
                disabled_capability, str(disabled_capability),
            )

            current_cap = await asyncio.to_thread(
                _get_sharing_capability_by_url_sync, ctx, site_url
            )
            current_name = SHARING_CAPABILITY_REVERSE.get(
                current_cap, str(current_cap),
            )

            if _capability_allows_anonymous(current_cap):
                action = "remediated"
                err: str | None = None
                try:
                    await asyncio.to_thread(
                        _set_sharing_capability_sync, ctx, site_url, disabled_capability,
                    )
                    logger.info(
                        "Cascade: disabled anonymous sharing on %s (after private flip)",
                        site_url,
                    )
                except Exception as e:
                    action = "failed"
                    err = _sanitize_error(e)
                    logger.error(
                        "Cascade: failed to disable anonymous sharing on %s: %s",
                        site_url, e,
                    )

                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO site_policy_events
                            (scan_id, policy_type, site_url, site_display_name,
                             group_id, previous_value, new_value, action, error_message)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        scan_id, "sharing", site_url, site_display_name,
                        group_id, current_name, desired_name, action, err,
                    )
    except Exception:
        logger.exception(
            "Cascade: anonymous-sharing cascade failed (non-fatal) for %s",
            site_url,
        )

    # --- Cascade 2: CWS drift event for the daily drift remediation --------
    try:
        async with db_pool.acquire() as conn:
            cws_allowed = await conn.fetchrow(
                "SELECT 1 FROM site_company_wide_sharing_allowlist WHERE LOWER(RTRIM(site_url, '/')) = $1 LIMIT 1",
                norm_url,
            )
        if cws_allowed:
            return
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cws_verification_events
                    (scan_id, site_url, site_display_name, user_upn,
                     expected_value, actual_value, drift_type,
                     exempt_reason, trigger_source, error_message)
                VALUES (NULL, $1, $2, '', 'Disabled', 'unknown', 'drift',
                        '', 'visibility_flip', NULL)
                """,
                site_url, site_display_name or "",
            )
        logger.info(
            "Cascade: recorded CWS drift event for %s (will be remediated by next drift run)",
            site_url,
        )
    except Exception:
        logger.exception(
            "Cascade: CWS drift recording failed (non-fatal) for %s", site_url,
        )


async def run_site_policy_scan(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
    scan_id: int,
) -> dict:
    """Execute one full site policy scan: enumerate, evaluate, remediate.

    Returns stats dict with counts of violations found, remediated, and errors.
    """
    stats = {
        "total_sites_scanned": 0,
        "visibility_violations_found": 0,
        "visibility_remediated": 0,
        "sharing_violations_found": 0,
        "sharing_remediated": 0,
        "errors": 0,
    }

    try:
        # Claim scan row
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_policy_scans
                SET status = 'in_progress', started_at = $1
                WHERE id = $2
                """,
                datetime.now(timezone.utc),
                scan_id,
            )

        # -- Phase 1: Enumerate sites --

        # 1a. Fetch all M365 groups (for visibility policy)
        all_groups = await enumerate_m365_groups(auth)

        # All Unified groups get a SharePoint site automatically.
        # For visibility enforcement, we only care about Public groups
        # (to set them Private). Skip HiddenMembership/null visibility.
        public_groups = [
            g for g in all_groups
            if (g.get("visibility") or "").lower() == "public"
        ]
        logger.info(
            "Phase 1: %d M365 groups total, %d Public",
            len(all_groups), len(public_groups),
        )

        # Only resolve site URLs for Public groups (optimization: skip
        # the other 24K+ Private groups since we never change them).
        # Site URLs are informational for logging/display only.
        public_group_ids = [g["id"] for g in public_groups]
        group_site_map: dict[str, str] = {}
        if public_group_ids:
            group_site_map = await batch_get_group_sites(auth, public_group_ids)

        # Build lookup: site_url (lowercase, no trailing slash) -> group info
        group_by_site: dict[str, dict] = {}
        for g in public_groups:
            site_url = group_site_map.get(g["id"], "")
            if site_url:
                key = site_url.rstrip("/").lower()
                group_by_site[key] = {
                    "group_id": g["id"],
                    "display_name": g.get("displayName", ""),
                    "visibility": g.get("visibility", ""),
                    "site_url": site_url,
                }

        # 1b. Fetch all SharePoint site collections (for sharing policy) via
        # the paged PnP inventory. The daily scan covers SharePoint sites;
        # OneDrive personal sites are covered by the separate weekly
        # run_onedrive_sharing_scan (site_policy_include_onedrive gates it).
        ctx = await asyncio.to_thread(_create_spo_admin_context, config)
        all_spo_sites = await get_site_inventory(
            config, include_onedrive=False, log_prefix="site-inventory",
        )

        spo_sites = [
            s for s in all_spo_sites
            if s.url and not _is_unsupported_site_type(s.url)
        ]
        stats["total_sites_scanned"] = len(spo_sites)
        logger.info("Phase 1: %d SPO sites (SharePoint only)", len(spo_sites))

        # Enumeration floor: a truncated inventory must fail the scan loudly
        # rather than read as a clean pass (the pre-2026-07 CSOM enumeration
        # silently returned only its first page).
        await _check_enumeration_floor(
            db_pool, "scheduled", len(spo_sites), config,
        )

        # -- Phase 2: Evaluate & remediate visibility --

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT group_id FROM site_visibility_allowlist")
        # GUIDs are compared case-insensitively — Graph returns lowercase but
        # dashboard-entered values may be uppercase/mixed.
        allowed_group_ids = {(r["group_id"] or "").strip().lower() for r in rows}

        logger.info(
            "Phase 2: Checking visibility (%d public groups, %d allowed)",
            len(public_groups), len(allowed_group_ids),
        )

        for group in public_groups:
            gid = group["id"]
            visibility = (group.get("visibility") or "").capitalize()
            display_name = group.get("displayName", "")
            site_url = group_site_map.get(gid, "")

            if visibility != "Public":
                continue  # Already private or unknown -- skip

            if gid.strip().lower() in allowed_group_ids:
                continue  # Explicitly allowed to be public

            # Violation: public group not on allow list
            stats["visibility_violations_found"] += 1
            action = "remediated"
            error_msg = None

            try:
                await set_group_visibility(auth, gid, "Private")
                stats["visibility_remediated"] += 1
            except Exception as e:
                action = "failed"
                error_msg = _sanitize_error(e)
                stats["errors"] += 1
                logger.error("Failed to set group %s to Private: %s", gid, e)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO site_policy_events
                        (scan_id, policy_type, site_url, site_display_name,
                         group_id, previous_value, new_value, action, error_message)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    scan_id, "visibility", site_url, display_name,
                    gid, "Public", "Private", action, error_msg,
                )

            # On successful flip, cascade: disable anonymous sharing + queue
            # CWS drift remediation for the same site. Safety net so newly
            # public-then-private sites don't sit with the other two policies
            # in their default-permissive state until the next full sweep.
            if action == "remediated" and site_url:
                await cascade_policies_on_private_flip(
                    db_pool, config, ctx,
                    scan_id=scan_id,
                    site_url=site_url,
                    site_display_name=display_name,
                    group_id=gid,
                )

            await asyncio.sleep(0.2)  # Rate limit

        # -- Phase 3: Evaluate & remediate sharing --
        await _enforce_sharing_on_sites(
            db_pool, config, ctx,
            scan_id=scan_id,
            sites=spo_sites,
            group_by_site=group_by_site,
            stats=stats,
            log_prefix="Phase 3",
        )

        # Mark scan completed
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_policy_scans
                SET status = 'completed',
                    completed_at = $1,
                    total_sites_scanned = $2,
                    visibility_violations_found = $3,
                    visibility_remediated = $4,
                    sharing_violations_found = $5,
                    sharing_remediated = $6,
                    errors = $7
                WHERE id = $8
                """,
                datetime.now(timezone.utc),
                stats["total_sites_scanned"],
                stats["visibility_violations_found"],
                stats["visibility_remediated"],
                stats["sharing_violations_found"],
                stats["sharing_remediated"],
                stats["errors"],
                scan_id,
            )

    except Exception as e:
        logger.exception("Site policy scan failed (scan_id=%d)", scan_id)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_policy_scans
                SET status = 'failed',
                    completed_at = $1,
                    error_message = $2,
                    total_sites_scanned = $3,
                    visibility_violations_found = $4,
                    visibility_remediated = $5,
                    sharing_violations_found = $6,
                    sharing_remediated = $7,
                    errors = $8
                WHERE id = $9
                """,
                datetime.now(timezone.utc),
                _sanitize_error(e),
                stats["total_sites_scanned"],
                stats["visibility_violations_found"],
                stats["visibility_remediated"],
                stats["sharing_violations_found"],
                stats["sharing_remediated"],
                stats["errors"],
                scan_id,
            )

    logger.info("Site policy scan complete (scan_id=%d): %s", scan_id, stats)
    return stats


async def run_onedrive_sharing_scan(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
    scan_id: int,
) -> dict:
    """Weekly OneDrive sharing-capability sweep.

    OneDrive personal sites are subject to the same anonymous-sharing policy
    as SharePoint sites, but there are ~150K+ of them, so they get their own
    weekly cadence (site_policy_onedrive_interval_hours) instead of riding the
    daily scan. Event-driven remediation (AnonymousLinkCreated) covers the gap
    between sweeps. Records under trigger_type='scheduled_onedrive'.
    """
    stats = {
        "total_sites_scanned": 0,
        "visibility_violations_found": 0,
        "visibility_remediated": 0,
        "sharing_violations_found": 0,
        "sharing_remediated": 0,
        "errors": 0,
    }

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_policy_scans
                SET status = 'in_progress', started_at = $1
                WHERE id = $2
                """,
                datetime.now(timezone.utc),
                scan_id,
            )

        ctx = await asyncio.to_thread(_create_spo_admin_context, config)
        all_sites = await get_site_inventory(
            config, onedrive_only=True, log_prefix="onedrive-inventory",
        )
        onedrive_sites = [
            s for s in all_sites
            if s.url and not _is_unsupported_site_type(s.url)
        ]
        stats["total_sites_scanned"] = len(onedrive_sites)
        logger.info("OneDrive sweep: %d personal sites enumerated", len(onedrive_sites))

        await _check_enumeration_floor(
            db_pool, "scheduled_onedrive", len(onedrive_sites), config,
        )

        await _enforce_sharing_on_sites(
            db_pool, config, ctx,
            scan_id=scan_id,
            sites=onedrive_sites,
            group_by_site={},
            stats=stats,
            log_prefix="OneDrive sweep",
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_policy_scans
                SET status = 'completed',
                    completed_at = $1,
                    total_sites_scanned = $2,
                    sharing_violations_found = $3,
                    sharing_remediated = $4,
                    errors = $5
                WHERE id = $6
                """,
                datetime.now(timezone.utc),
                stats["total_sites_scanned"],
                stats["sharing_violations_found"],
                stats["sharing_remediated"],
                stats["errors"],
                scan_id,
            )

    except Exception as e:
        logger.exception("OneDrive sharing sweep failed (scan_id=%d)", scan_id)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE site_policy_scans
                SET status = 'failed',
                    completed_at = $1,
                    error_message = $2,
                    total_sites_scanned = $3,
                    sharing_violations_found = $4,
                    sharing_remediated = $5,
                    errors = $6
                WHERE id = $7
                """,
                datetime.now(timezone.utc),
                _sanitize_error(e),
                stats["total_sites_scanned"],
                stats["sharing_violations_found"],
                stats["sharing_remediated"],
                stats["errors"],
                scan_id,
            )

    logger.info("OneDrive sharing sweep complete (scan_id=%d): %s", scan_id, stats)
    return stats


async def apply_visibility_for_group(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    group_id: str,
    group_display_name: str,
    site_url: str,
    triggered_by: str,
) -> dict:
    """Set a single group to Public after it was added to the visibility allow list.

    Creates an ad-hoc scan row and logs the event.
    """
    now = datetime.now(timezone.utc)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_policy_scans
                (trigger_type, triggered_by, status, started_at)
            VALUES ('allowlist_add', $1, 'in_progress', $2)
            RETURNING id
            """,
            triggered_by, now,
        )
    scan_id = row["id"]

    action = "remediated"
    error_msg = None
    try:
        from .graph_api import set_group_visibility
        await set_group_visibility(auth, group_id, "Public")
        logger.info("Set group %s (%s) to Public (allowlist add)", group_id, group_display_name)
    except Exception as e:
        action = "failed"
        error_msg = _sanitize_error(e)
        logger.error("Failed to set group %s to Public: %s", group_id, e)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO site_policy_events
                (scan_id, policy_type, site_url, site_display_name,
                 group_id, previous_value, new_value, action, error_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            scan_id, "visibility", site_url, group_display_name,
            group_id, "Private", "Public", action, error_msg,
        )
        status = "completed" if action == "remediated" else "failed"
        await conn.execute(
            """
            UPDATE site_policy_scans
            SET status = $1::VARCHAR, completed_at = $2,
                visibility_remediated = CASE WHEN $1::VARCHAR = 'completed' THEN 1 ELSE 0 END,
                errors = CASE WHEN $1::VARCHAR = 'failed' THEN 1 ELSE 0 END,
                error_message = $3
            WHERE id = $4
            """,
            status, datetime.now(timezone.utc), error_msg, scan_id,
        )

    return {"scan_id": scan_id, "action": action, "error": error_msg}


async def apply_sharing_for_site(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
    site_url: str,
    site_display_name: str,
    triggered_by: str,
) -> dict:
    """Enable anonymous sharing on a single site after it was added to the sharing allow list.

    Creates an ad-hoc scan row and logs the event.
    """
    now = datetime.now(timezone.utc)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_policy_scans
                (trigger_type, triggered_by, status, started_at)
            VALUES ('allowlist_add', $1, 'in_progress', $2)
            RETURNING id
            """,
            triggered_by, now,
        )
    scan_id = row["id"]

    enabled_capability = SHARING_CAPABILITY_MAP.get(
        config.site_policy_enabled_sharing_capability,
        SHARING_CAPABILITY_MAP["ExternalUserAndGuestSharing"],
    )
    desired_name = SHARING_CAPABILITY_REVERSE.get(
        enabled_capability, str(enabled_capability)
    )

    action = "remediated"
    error_msg = None
    previous_name = "unknown"
    try:
        ctx = await asyncio.to_thread(_create_spo_admin_context, config)
        # Get current capability for logging
        from office365.sharepoint.tenant.administration.tenant import Tenant
        def _get_current(ctx, url):
            tenant = Tenant(ctx)
            props = tenant.get_site_properties_by_url(_encode_csom_url(url), True)
            ctx.execute_query()
            return props.sharing_capability
        current_cap = await asyncio.to_thread(_get_current, ctx, site_url)
        previous_name = SHARING_CAPABILITY_REVERSE.get(current_cap, str(current_cap))

        if current_cap != enabled_capability:
            await asyncio.to_thread(
                _set_sharing_capability_sync, ctx, site_url, enabled_capability
            )
            logger.info("Enabled sharing on %s (allowlist add)", site_url)
        else:
            action = "already_compliant"
            logger.info("Sharing already enabled on %s", site_url)
    except Exception as e:
        action = "failed"
        error_msg = _sanitize_error(e)
        logger.error("Failed to enable sharing on %s: %s", site_url, e)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO site_policy_events
                (scan_id, policy_type, site_url, site_display_name,
                 group_id, previous_value, new_value, action, error_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            scan_id, "sharing", site_url, site_display_name,
            "", previous_name, desired_name, action, error_msg,
        )
        status = "completed" if action != "failed" else "failed"
        await conn.execute(
            """
            UPDATE site_policy_scans
            SET status = $1::VARCHAR, completed_at = $2,
                sharing_remediated = CASE WHEN $3::VARCHAR = 'remediated' THEN 1 ELSE 0 END,
                errors = CASE WHEN $1::VARCHAR = 'failed' THEN 1 ELSE 0 END,
                error_message = $4
            WHERE id = $5
            """,
            status, datetime.now(timezone.utc), action, error_msg, scan_id,
        )

    return {"scan_id": scan_id, "action": action, "error": error_msg}


async def revoke_visibility_for_group(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
    group_id: str,
    group_display_name: str,
    site_url: str,
    triggered_by: str,
) -> dict:
    """Set a single group back to Private after it was removed from the visibility allow list."""
    now = datetime.now(timezone.utc)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_policy_scans
                (trigger_type, triggered_by, status, started_at)
            VALUES ('allowlist_remove', $1, 'in_progress', $2)
            RETURNING id
            """,
            triggered_by, now,
        )
    scan_id = row["id"]

    action = "remediated"
    error_msg = None
    try:
        from .graph_api import set_group_visibility
        await set_group_visibility(auth, group_id, "Private")
        logger.info("Set group %s (%s) to Private (allowlist remove)", group_id, group_display_name)
    except Exception as e:
        action = "failed"
        error_msg = _sanitize_error(e)
        logger.error("Failed to set group %s to Private: %s", group_id, e)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO site_policy_events
                (scan_id, policy_type, site_url, site_display_name,
                 group_id, previous_value, new_value, action, error_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            scan_id, "visibility", site_url, group_display_name,
            group_id, "Public", "Private", action, error_msg,
        )
        status = "completed" if action == "remediated" else "failed"
        await conn.execute(
            """
            UPDATE site_policy_scans
            SET status = $1::VARCHAR, completed_at = $2,
                visibility_remediated = CASE WHEN $1::VARCHAR = 'completed' THEN 1 ELSE 0 END,
                errors = CASE WHEN $1::VARCHAR = 'failed' THEN 1 ELSE 0 END,
                error_message = $3
            WHERE id = $4
            """,
            status, datetime.now(timezone.utc), error_msg, scan_id,
        )

    # Cascade the other two policies (anonymous sharing + CWS) on success.
    if action == "remediated" and site_url:
        try:
            ctx = await asyncio.to_thread(_create_spo_admin_context, config)
            await cascade_policies_on_private_flip(
                db_pool, config, ctx,
                scan_id=scan_id,
                site_url=site_url,
                site_display_name=group_display_name,
                group_id=group_id,
            )
        except Exception:
            logger.exception(
                "Cascade after dashboard set_private failed (non-fatal) for %s",
                site_url,
            )

    return {"scan_id": scan_id, "action": action, "error": error_msg}


async def revoke_sharing_for_site(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
    site_url: str,
    site_display_name: str,
    triggered_by: str,
) -> dict:
    """Disable anonymous sharing on a single site after it was removed from the sharing allow list."""
    now = datetime.now(timezone.utc)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_policy_scans
                (trigger_type, triggered_by, status, started_at)
            VALUES ('allowlist_remove', $1, 'in_progress', $2)
            RETURNING id
            """,
            triggered_by, now,
        )
    scan_id = row["id"]

    disabled_capability = SHARING_CAPABILITY_MAP.get(
        config.site_policy_disabled_sharing_capability,
        SHARING_CAPABILITY_MAP["ExternalUserSharingOnly"],
    )
    desired_name = SHARING_CAPABILITY_REVERSE.get(
        disabled_capability, str(disabled_capability)
    )

    action = "remediated"
    error_msg = None
    previous_name = "unknown"
    try:
        ctx = await asyncio.to_thread(_create_spo_admin_context, config)
        from office365.sharepoint.tenant.administration.tenant import Tenant
        def _get_current(ctx, url):
            tenant = Tenant(ctx)
            props = tenant.get_site_properties_by_url(_encode_csom_url(url), True)
            ctx.execute_query()
            return props.sharing_capability
        current_cap = await asyncio.to_thread(_get_current, ctx, site_url)
        previous_name = SHARING_CAPABILITY_REVERSE.get(current_cap, str(current_cap))

        # Never loosen: only downgrade when the current capability actually
        # allows anonymous links. A site already at Disabled(0) or
        # ExistingExternalUserSharingOnly(3) is stricter than our target —
        # setting it to ExternalUserSharingOnly(1) would RAISE its sharing
        # surface (this action arrives from event/provisioning triggers for
        # every new non-allow-listed site, most of which are born strict).
        if _capability_allows_anonymous(current_cap):
            await asyncio.to_thread(
                _set_sharing_capability_sync, ctx, site_url, disabled_capability
            )
            logger.info("Disabled sharing on %s (allowlist remove)", site_url)
        else:
            action = "already_compliant"
            logger.info(
                "Sharing on %s already at-or-stricter-than target (%s) — no change",
                site_url, previous_name,
            )
    except Exception as e:
        action = "failed"
        error_msg = _sanitize_error(e)
        logger.error("Failed to disable sharing on %s: %s", site_url, e)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO site_policy_events
                (scan_id, policy_type, site_url, site_display_name,
                 group_id, previous_value, new_value, action, error_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            scan_id, "sharing", site_url, site_display_name,
            "", previous_name, desired_name, action, error_msg,
        )
        status = "completed" if action != "failed" else "failed"
        await conn.execute(
            """
            UPDATE site_policy_scans
            SET status = $1::VARCHAR, completed_at = $2,
                sharing_remediated = CASE WHEN $3::VARCHAR = 'remediated' THEN 1 ELSE 0 END,
                errors = CASE WHEN $1::VARCHAR = 'failed' THEN 1 ELSE 0 END,
                error_message = $4
            WHERE id = $5
            """,
            status, datetime.now(timezone.utc), action, error_msg, scan_id,
        )

    return {"scan_id": scan_id, "action": action, "error": error_msg}
