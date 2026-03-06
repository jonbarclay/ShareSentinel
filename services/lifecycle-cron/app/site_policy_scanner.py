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
    _capability_allows_anonymous,
    _create_spo_admin_context,
    _get_all_site_properties_sync,
    _get_sharing_capability_sync,
    _set_sharing_capability_sync,
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

        # 1b. Fetch all SPO site collections (for sharing policy)
        ctx = await asyncio.to_thread(_create_spo_admin_context, config)
        all_spo_sites = await asyncio.to_thread(_get_all_site_properties_sync, ctx)

        # Filter out personal OneDrive sites
        spo_sites = [
            s for s in all_spo_sites
            if s.url and "/personal/" not in str(s.url).lower()
        ]
        stats["total_sites_scanned"] = len(spo_sites)
        logger.info("Phase 1: %d SPO sites (excl. OneDrive)", len(spo_sites))

        # -- Phase 2: Evaluate & remediate visibility --

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT group_id FROM site_visibility_allowlist")
        allowed_group_ids = {r["group_id"] for r in rows}

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

            if gid in allowed_group_ids:
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

            await asyncio.sleep(0.2)  # Rate limit

        # -- Phase 3: Evaluate & remediate sharing --

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT site_url FROM site_allowlist")
        allowed_sharing_urls = {
            r["site_url"].rstrip("/").lower() for r in rows
        }

        enabled_capability = SHARING_CAPABILITY_MAP.get(
            config.site_policy_enabled_sharing_capability,
            SHARING_CAPABILITY_MAP["ExternalUserAndGuestSharing"],
        )
        disabled_capability = SHARING_CAPABILITY_MAP.get(
            config.site_policy_disabled_sharing_capability,
            SHARING_CAPABILITY_MAP["ExternalUserSharingOnly"],
        )

        logger.info(
            "Phase 3: Checking sharing (%d sites, %d allowed)",
            len(spo_sites), len(allowed_sharing_urls),
        )

        for site_props in spo_sites:
            site_url = str(site_props.url).rstrip("/").lower() if site_props.url else ""
            site_display_name = str(site_props.title) if hasattr(site_props, "title") else ""
            current_capability = _get_sharing_capability_sync(site_props)
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
            props = tenant.get_site_properties_by_url(url, True)
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
            props = tenant.get_site_properties_by_url(url, True)
            ctx.execute_query()
            return props.sharing_capability
        current_cap = await asyncio.to_thread(_get_current, ctx, site_url)
        previous_name = SHARING_CAPABILITY_REVERSE.get(current_cap, str(current_cap))

        if current_cap != disabled_capability:
            await asyncio.to_thread(
                _set_sharing_capability_sync, ctx, site_url, disabled_capability
            )
            logger.info("Disabled sharing on %s (allowlist remove)", site_url)
        else:
            action = "already_compliant"
            logger.info("Sharing already disabled on %s", site_url)
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
