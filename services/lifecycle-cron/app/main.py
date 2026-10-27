"""Entry point for the lifecycle cron service.

Runs concurrent loops:
1. Lifecycle processor — checks sharing link expiry milestones (daily)
2. Audit log poller — queries Graph API audit logs for new sharing events (every 15m)
3. Site policy enforcer — enforces visibility + sharing policies (daily + manual)
4. Folder rescan — re-checks shared folders for new/modified files (weekly)
5. CWS verification sweep — PowerShell, detect-only daily site enumeration
6. CWS drift remediation — PowerShell, daily drain of unresolved drift rows
7. CWS bulk remediation — PowerShell, monthly full-tenant safety net
8. CWS exception group sync — 10-min Graph poll, fires PS only on membership delta
9. CWS allowlist action — drains dashboard-pushed cws_allow_site / cws_disallow_site
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis

from .config import LifecycleConfig
from .graph_api import GraphAuth
from .processor import process_lifecycle_milestones
from .pwsh import SCRIPTS_DIR, run_pwsh_script as _run_pwsh_script  # noqa: F401

logger = logging.getLogger(__name__)

# -- Heartbeat ---------------------------------------------------------------

HEARTBEAT_INTERVAL_S = 60
HEARTBEAT_KEYS = {
    "audit_poller": "sharesentinel:lifecycle:heartbeat:audit_poller",
    "lifecycle": "sharesentinel:lifecycle:heartbeat:lifecycle",
    "site_policy": "sharesentinel:lifecycle:heartbeat:site_policy",
    "folder_rescan": "sharesentinel:lifecycle:heartbeat:folder_rescan",
    "cws_verification": "sharesentinel:lifecycle:heartbeat:cws_verification",
    "cws_drift_remediation": "sharesentinel:lifecycle:heartbeat:cws_drift_remediation",
    "cws_bulk_remediation": "sharesentinel:lifecycle:heartbeat:cws_bulk_remediation",
    "cws_exception_sync": "sharesentinel:lifecycle:heartbeat:cws_exception_sync",
    "cws_allowlist_action": "sharesentinel:lifecycle:heartbeat:cws_allowlist_action",
    "cws_failure_diagnostic": "sharesentinel:lifecycle:heartbeat:cws_failure_diagnostic",
}

# Mirror of the CWS exception group membership, read by the worker's
# event-driven drift detector (cws_drift_detector.EXCEPTION_UPN_CACHE_KEY) to
# exempt members' own OneDrives from CompanyLinkCreated remediation. Refreshed
# every exception-group sync tick.
CWS_EXCEPTION_UPN_CACHE_KEY = "sharesentinel:cws_exception_upns"

# Shared Redis lock so a CWS PowerShell script never runs concurrently with
# another one (they all hit the same tenant admin API and share throttle budget).
CWS_REMEDIATION_LOCK_KEY = "sharesentinel:cws_remediation_lock"
CWS_REMEDIATION_LOCK_TTL_S = 6 * 3600  # 6 hours; bulk can take hours at tenant scale

# _run_pwsh_script / SCRIPTS_DIR live in .pwsh (shared with the site policy
# scanner's inventory loader); imported at the top of this module.


async def _heartbeat_task(redis_client: aioredis.Redis, loop_name: str) -> None:
    """Write a heartbeat timestamp to Redis every HEARTBEAT_INTERVAL_S seconds."""
    key = HEARTBEAT_KEYS[loop_name]
    while True:
        try:
            await redis_client.set(key, str(int(time.time())))
        except Exception:
            logger.warning("Failed to write heartbeat for %s", loop_name, exc_info=True)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _acquire_cws_lock(redis_client: aioredis.Redis, holder: str) -> bool:
    """Best-effort single-flight across all CWS PowerShell loops."""
    try:
        ok = await redis_client.set(
            CWS_REMEDIATION_LOCK_KEY, holder,
            nx=True, ex=CWS_REMEDIATION_LOCK_TTL_S,
        )
        return bool(ok)
    except Exception:
        logger.warning("Failed to acquire CWS remediation lock", exc_info=True)
        return False


async def _refresh_exception_upn_cache(
    redis_client: aioredis.Redis, upns: set[str]
) -> None:
    """Mirror the exception group membership into Redis for the worker.

    Swaps the set atomically (temp key + RENAME) so the worker's drift
    detector never observes a half-written set. An empty group deletes the
    key, which the worker treats as "nobody is exempt".
    """
    tmp_key = f"{CWS_EXCEPTION_UPN_CACHE_KEY}:staging"
    try:
        if upns:
            pipe = redis_client.pipeline(transaction=True)
            pipe.delete(tmp_key)
            pipe.sadd(tmp_key, *upns)
            pipe.rename(tmp_key, CWS_EXCEPTION_UPN_CACHE_KEY)
            await pipe.execute()
        else:
            await redis_client.delete(CWS_EXCEPTION_UPN_CACHE_KEY)
    except Exception:
        logger.warning("Failed to refresh CWS exception UPN cache", exc_info=True)


async def _release_cws_lock(redis_client: aioredis.Redis, holder: str) -> None:
    try:
        current = await redis_client.get(CWS_REMEDIATION_LOCK_KEY)
        # Only release if we own the lock (TTL covers crash recovery).
        if current and (current.decode() if isinstance(current, bytes) else current) == holder:
            await redis_client.delete(CWS_REMEDIATION_LOCK_KEY)
    except Exception:
        logger.warning("Failed to release CWS remediation lock", exc_info=True)


async def _last_completed_at(
    db_pool: asyncpg.Pool,
    table: str,
    trigger_type: str,
) -> datetime | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT completed_at FROM {table}
            WHERE trigger_type = $1 AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            trigger_type,
        )
    return row["completed_at"] if row and row["completed_at"] else None


async def _last_attempt_at(
    db_pool: asyncpg.Pool,
    table: str,
    trigger_type: str,
) -> datetime | None:
    """Most recent attempt of any status — used to back off after a failed
    scheduled scan instead of re-running it every poll tick."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT created_at FROM {table}
            WHERE trigger_type = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            trigger_type,
        )
    return row["created_at"] if row and row["created_at"] else None


FAILED_SCAN_RETRY_BACKOFF_S = 3600  # 1h between attempts of a failing scheduled scan


async def _scheduled_scan_due(
    db_pool: asyncpg.Pool,
    table: str,
    trigger_type: str,
    interval_seconds: int,
) -> bool:
    now = datetime.now(timezone.utc)
    last_ok = await _last_completed_at(db_pool, table, trigger_type)
    if last_ok and (now - last_ok).total_seconds() < interval_seconds:
        return False
    last_try = await _last_attempt_at(db_pool, table, trigger_type)
    if last_try and (now - last_try).total_seconds() < FAILED_SCAN_RETRY_BACKOFF_S:
        return False
    return True


async def lifecycle_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Run the lifecycle milestone processor on a fixed interval."""
    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "lifecycle"))

    interval_seconds = config.check_interval_hours * 3600
    while True:
        try:
            stats = await process_lifecycle_milestones(db_pool, auth, config)
            logger.info("Lifecycle cycle complete: %s", stats)
        except Exception:
            logger.exception("Error in lifecycle processing cycle")
        logger.info("Lifecycle sleeping %d seconds until next cycle", interval_seconds)
        await asyncio.sleep(interval_seconds)


async def site_policy_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Daily site policy scanner + enforcer.

    Polls Redis every 60s for manual triggers, runs scheduled scans at configured interval.
    Handles both visibility (Public->Private) and sharing (anonymous link) enforcement.
    """
    from .site_policy_scanner import (
        apply_sharing_for_site,
        apply_visibility_for_group,
        revoke_sharing_for_site,
        revoke_visibility_for_group,
        run_onedrive_sharing_scan,
        run_site_policy_scan,
    )

    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "site_policy"))

    interval_seconds = config.site_policy_interval_hours * 3600

    logger.info(
        "Site policy loop starting (interval=%dh)",
        config.site_policy_interval_hours,
    )

    _ALLOWED_ACTIONS = {"set_public", "set_private", "enable_sharing", "disable_sharing"}
    _UUID_RE = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I,
    )

    while True:
        try:
            # 1a. Check Redis for targeted actions (add-to-allowlist triggers)
            action_msg = await redis_client.lpop("sharesentinel:site_policy_action")
            if action_msg:
                try:
                    action_data = json.loads(action_msg)
                    action_type = action_data.get("action")

                    if action_type not in _ALLOWED_ACTIONS:
                        logger.warning("Rejected unknown site policy action: %s", action_type)
                        continue

                    if action_type in ("set_public", "set_private"):
                        gid = action_data.get("group_id", "")
                        if not _UUID_RE.match(gid):
                            logger.warning("Rejected invalid group_id: %s", gid[:50])
                            continue

                    if action_type in ("enable_sharing", "disable_sharing"):
                        url = action_data.get("site_url", "")
                        if not url.startswith("https://"):
                            logger.warning("Rejected invalid site_url: %s", url[:80])
                            continue

                    logger.info("Site policy action received: %s", action_type)

                    if action_type == "set_public":
                        await apply_visibility_for_group(
                            db_pool, auth,
                            group_id=action_data["group_id"],
                            group_display_name=action_data.get("group_display_name", ""),
                            site_url=action_data.get("site_url", ""),
                            triggered_by=action_data.get("triggered_by", "unknown"),
                        )
                    elif action_type == "enable_sharing":
                        await apply_sharing_for_site(
                            db_pool, config,
                            site_url=action_data["site_url"],
                            site_display_name=action_data.get("site_display_name", ""),
                            triggered_by=action_data.get("triggered_by", "unknown"),
                        )
                    elif action_type == "set_private":
                        await revoke_visibility_for_group(
                            db_pool, auth, config,
                            group_id=action_data["group_id"],
                            group_display_name=action_data.get("group_display_name", ""),
                            site_url=action_data.get("site_url", ""),
                            triggered_by=action_data.get("triggered_by", "unknown"),
                        )
                    elif action_type == "disable_sharing":
                        await revoke_sharing_for_site(
                            db_pool, config,
                            site_url=action_data["site_url"],
                            site_display_name=action_data.get("site_display_name", ""),
                            triggered_by=action_data.get("triggered_by", "unknown"),
                        )
                except Exception:
                    # The message was already popped — park it on a dead-letter
                    # list instead of dropping it, so a transient failure
                    # doesn't silently lose an enforcement action. (The apply/
                    # revoke helpers record their own failures; this catches
                    # crashes before/around them.)
                    logger.exception(
                        "Site policy action failed — dead-lettering message",
                    )
                    try:
                        await redis_client.rpush(
                            "sharesentinel:site_policy_action:dlq", action_msg,
                        )
                    except Exception:
                        logger.exception("Failed to dead-letter site policy action")

                continue

            # 1b. Check Redis for manual full scan trigger
            trigger = await redis_client.lpop("sharesentinel:site_policy_trigger")
            if trigger:
                data = json.loads(trigger)
                scan_id = data.get("scan_id")
                if not isinstance(scan_id, int) or scan_id < 1:
                    logger.warning("Rejected invalid scan_id: %s", scan_id)
                    continue
                logger.info("Manual site policy scan triggered (scan_id=%d)", scan_id)
                await run_site_policy_scan(db_pool, auth, config, scan_id=scan_id)
                continue

            # 2. Scheduled daily scan (visibility + SharePoint sharing)
            if await _scheduled_scan_due(
                db_pool, "site_policy_scans", "scheduled", interval_seconds,
            ):
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO site_policy_scans
                            (trigger_type, status)
                        VALUES ('scheduled', 'pending')
                        RETURNING id
                        """
                    )
                scan_id = row["id"]
                logger.info("Scheduled site policy scan starting (scan_id=%d)", scan_id)
                await run_site_policy_scan(db_pool, auth, config, scan_id=scan_id)
                continue

            # 3. Scheduled weekly OneDrive sharing sweep
            if config.site_policy_include_onedrive and await _scheduled_scan_due(
                db_pool,
                "site_policy_scans",
                "scheduled_onedrive",
                config.site_policy_onedrive_interval_hours * 3600,
            ):
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO site_policy_scans
                            (trigger_type, status)
                        VALUES ('scheduled_onedrive', 'pending')
                        RETURNING id
                        """
                    )
                scan_id = row["id"]
                logger.info("Scheduled OneDrive sharing sweep starting (scan_id=%d)", scan_id)
                await run_onedrive_sharing_scan(db_pool, config, scan_id=scan_id)

        except Exception:
            logger.exception("Error in site policy loop")

        await asyncio.sleep(60)


async def cws_verification_loop(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> None:
    """PowerShell-based CWS verification sweep (detect-only).

    Uses the bulk `Get-PnPTenantSite -Detailed -IncludeOneDriveSites` enumeration
    only as the site inventory, then resolves each non-exempt site's real
    `DisableCompanyWideSharingLinks` value with a per-site `-Identity` call (the
    bulk value is always `Unknown`) via a pool of read-only worker threads, and
    writes a `cws_verification_events` drift row ONLY for genuinely non-Disabled
    sites — `Unknown`/error results are counted as unresolved, not drift. Polls
    Redis every 60s for manual triggers; otherwise starts a scheduled sweep once
    `cws_verification_interval_hours` has elapsed since the last completed
    scheduled sweep.

    Single-flight against drift + bulk remediation via a shared Redis lock so
    we never hammer the SPO admin API from two concurrent PS processes.
    """
    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "cws_verification"))

    interval_seconds = config.cws_verification_interval_hours * 3600
    logger.info(
        "CWS verification loop starting (interval=%dh, PowerShell)",
        config.cws_verification_interval_hours,
    )

    while True:
        try:
            trigger = await redis_client.lpop("sharesentinel:cws_verification_trigger")
            scan_id: int | None = None
            if trigger:
                data = json.loads(trigger)
                tid = data.get("scan_id")
                if not isinstance(tid, int) or tid < 1:
                    logger.warning("Rejected invalid CWS verification scan_id: %s", tid)
                    continue
                scan_id = tid
                logger.info("Manual CWS verification sweep triggered (scan_id=%d)", scan_id)
            else:
                last = await _last_completed_at(db_pool, "cws_verification_scans", "scheduled")
                run_due = True
                if last:
                    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                    run_due = elapsed >= interval_seconds
                if not run_due:
                    await asyncio.sleep(60)
                    continue
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO cws_verification_scans
                            (trigger_type, status)
                        VALUES ('scheduled', 'pending')
                        RETURNING id
                        """
                    )
                scan_id = row["id"]
                logger.info("Scheduled CWS verification sweep starting (scan_id=%d)", scan_id)

            holder = f"verification:{scan_id}"
            got_lock = await _acquire_cws_lock(redis_client, holder)
            if not got_lock:
                logger.info("CWS remediation lock held; skipping verification sweep this tick")
                # Reset the pending row so the next tick can pick it up.
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM cws_verification_scans WHERE id=$1 AND status='pending'",
                        scan_id,
                    )
                await asyncio.sleep(60)
                continue

            try:
                await _run_pwsh_script(
                    "Invoke-CwsVerificationSweep.ps1",
                    ["-ScanId", str(scan_id)],
                    "cws-verify",
                )
            finally:
                await _release_cws_lock(redis_client, holder)

        except Exception:
            logger.exception("Error in CWS verification loop")

        await asyncio.sleep(60)


async def cws_drift_remediation_loop(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> None:
    """Daily drain of unresolved CWS drift rows via PowerShell.

    Two trigger paths share the same Redis lock:
    - Manual: dashboard's "Scan Now" button pushes to
      ``sharesentinel:cws_drift_remediation_trigger`` with a pre-allocated
      ``company_wide_sharing_scans`` row id; PS script adopts the row via -ScanId.
    - Scheduled: fires when ``cws_drift_remediation_interval_hours`` has
      elapsed since the last completed ``drift_remediation`` scan.
    """
    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "cws_drift_remediation"))

    interval_seconds = config.cws_drift_remediation_interval_hours * 3600
    logger.info(
        "CWS drift remediation loop starting (interval=%dh, dry_run=%s)",
        config.cws_drift_remediation_interval_hours,
        config.cws_drift_remediation_dry_run,
    )

    while True:
        try:
            # 1. Manual trigger from the dashboard (highest priority).
            trigger = await redis_client.lpop("sharesentinel:cws_drift_remediation_trigger")
            manual_scan_id: int | None = None
            if trigger:
                data = json.loads(trigger)
                tid = data.get("scan_id")
                if not isinstance(tid, int) or tid < 1:
                    logger.warning("Rejected invalid drift remediation scan_id: %s", tid)
                else:
                    manual_scan_id = tid
                    logger.info("Manual CWS drift remediation triggered (scan_id=%d)", manual_scan_id)

            # 2. Scheduled run check (skipped when a manual trigger fired).
            if manual_scan_id is None:
                last = await _last_completed_at(
                    db_pool, "company_wide_sharing_scans", "drift_remediation",
                )
                run_due = True
                if last:
                    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                    run_due = elapsed >= interval_seconds
                if not run_due:
                    await asyncio.sleep(60)
                    continue

            holder = f"drift:{manual_scan_id or int(time.time())}"
            got_lock = await _acquire_cws_lock(redis_client, holder)
            if not got_lock:
                if manual_scan_id is not None:
                    # Re-queue so the next tick retries once the lock frees,
                    # and surface the wait to the UI via error_message.
                    await redis_client.rpush(
                        "sharesentinel:cws_drift_remediation_trigger",
                        json.dumps({"scan_id": manual_scan_id}),
                    )
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE company_wide_sharing_scans "
                            "SET error_message='waiting: another CWS PowerShell job is running' "
                            "WHERE id=$1 AND status='pending'",
                            manual_scan_id,
                        )
                    logger.info(
                        "CWS lock held; re-queued manual drift trigger scan_id=%d",
                        manual_scan_id,
                    )
                else:
                    logger.debug("CWS remediation lock held; deferring scheduled drift tick")
                await asyncio.sleep(60)
                continue
            try:
                args: list[str] = []
                if manual_scan_id is not None:
                    args += ["-ScanId", str(manual_scan_id)]
                else:
                    # pwsh -File treats "$true"/"$false" as literal strings and
                    # fails the [bool] cast; numeric 1/0 binds correctly.
                    dry_run = "1" if config.cws_drift_remediation_dry_run else "0"
                    args += ["-DryRun", dry_run]
                await _run_pwsh_script(
                    "Invoke-CwsDriftRemediation.ps1",
                    args,
                    "cws-drift",
                )
            finally:
                await _release_cws_lock(redis_client, holder)

        except Exception:
            logger.exception("Error in CWS drift remediation loop")

        await asyncio.sleep(60)


async def cws_bulk_remediation_loop(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> None:
    """Monthly full-tenant CWS bulk remediation via PowerShell (safety net)."""
    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "cws_bulk_remediation"))

    interval_seconds = config.cws_bulk_remediation_interval_hours * 3600
    logger.info(
        "CWS bulk remediation loop starting (interval=%dh, dry_run=%s, throttle=%d)",
        config.cws_bulk_remediation_interval_hours,
        config.cws_bulk_remediation_dry_run,
        config.cws_bulk_remediation_throttle,
    )

    while True:
        try:
            last = await _last_completed_at(
                db_pool, "company_wide_sharing_scans", "ps_bulk_tenant",
            )
            run_due = True
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                run_due = elapsed >= interval_seconds
            if not run_due:
                await asyncio.sleep(60)
                continue

            holder = f"bulk:{int(time.time())}"
            got_lock = await _acquire_cws_lock(redis_client, holder)
            if not got_lock:
                logger.info("CWS remediation lock held; deferring bulk remediation")
                await asyncio.sleep(60)
                continue
            try:
                # pwsh -File treats "$true"/"$false" as literal strings and
                # fails the [bool] cast; numeric 1/0 binds correctly.
                dry_run = "1" if config.cws_bulk_remediation_dry_run else "0"
                await _run_pwsh_script(
                    "Invoke-CwsBulkRemediation.ps1",
                    [
                        "-DryRun", dry_run,
                        "-ThrottleLimit", str(config.cws_bulk_remediation_throttle),
                    ],
                    "cws-bulk",
                )
            finally:
                await _release_cws_lock(redis_client, holder)

        except Exception:
            logger.exception("Error in CWS bulk remediation loop")

        # Bulk runs are long; poll less often than the other loops.
        await asyncio.sleep(300)


async def cws_allowlist_action_loop(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> None:
    """Drain dashboard-pushed CWS allowlist add/remove triggers.

    Consumer for the ``sharesentinel:company_wide_sharing_action`` Redis list,
    which the dashboard's add/remove endpoints push to. Each message is a
    single site URL with an action of ``cws_allow_site`` (re-enable CWS,
    DisableCompanyWideSharingLinks=NotDisabled) or ``cws_disallow_site``
    (disable, =Disabled). Allocates a scan row with
    trigger_type='allowlist_action' and runs Invoke-CwsAllowlistAction.ps1.

    Polls Redis every few seconds; the script honors the shared CWS lock so it
    never collides with the bulk/drift/verification PowerShell jobs.
    """
    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "cws_allowlist_action"))

    _ALLOWED_ACTIONS = {"cws_allow_site", "cws_disallow_site"}

    logger.info("CWS allowlist action loop starting")

    while True:
        try:
            msg = await redis_client.lpop("sharesentinel:company_wide_sharing_action")
            if not msg:
                await asyncio.sleep(5)
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                preview = (msg[:200] if isinstance(msg, (bytes, bytearray)) else str(msg)[:200])
                logger.warning("Discarding malformed CWS allowlist message: %r", preview)
                continue

            action = data.get("action", "")
            site_url = data.get("site_url", "")
            site_display_name = data.get("site_display_name", "") or ""
            triggered_by = data.get("triggered_by", "") or "dashboard"

            if action not in _ALLOWED_ACTIONS:
                logger.warning("Rejected unknown CWS allowlist action: %s", action)
                continue
            if not site_url.startswith("https://"):
                logger.warning("Rejected invalid site_url: %s", site_url[:80])
                continue

            logger.info(
                "CWS allowlist action received: %s for %s (by %s)",
                action, site_url, triggered_by,
            )

            holder = f"allowlist_action:{int(time.time() * 1000)}"
            got_lock = await _acquire_cws_lock(redis_client, holder)
            if not got_lock:
                # Put the message back at the head so it's next to process,
                # then back off briefly. (Single-instance lifecycle-cron, so
                # head-reinsert preserves order.)
                await redis_client.lpush(
                    "sharesentinel:company_wide_sharing_action", msg,
                )
                logger.info("CWS lock held; re-queued allowlist action for next tick")
                await asyncio.sleep(15)
                continue

            try:
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO company_wide_sharing_scans
                            (trigger_type, triggered_by, status, dry_run)
                        VALUES ('allowlist_action', $1, 'pending', false)
                        RETURNING id
                        """,
                        triggered_by,
                    )
                scan_id = row["id"]

                rc, _stdout, _stderr = await _run_pwsh_script(
                    "Invoke-CwsAllowlistAction.ps1",
                    [
                        "-Action", action,
                        "-SiteUrl", site_url,
                        "-SiteDisplayName", site_display_name,
                        "-TriggeredBy", triggered_by,
                        "-ScanId", str(scan_id),
                    ],
                    "cws-allowlist-action",
                )
                if rc != 0:
                    # Script records its own failure detail on the scan row;
                    # dead-letter the message so the action isn't lost.
                    logger.error(
                        "CWS allowlist action script rc=%d — dead-lettering %s for %s",
                        rc, action, site_url,
                    )
                    await redis_client.rpush(
                        "sharesentinel:company_wide_sharing_action:dlq", msg,
                    )
            except Exception:
                logger.exception(
                    "CWS allowlist action failed — dead-lettering message",
                )
                try:
                    await redis_client.rpush(
                        "sharesentinel:company_wide_sharing_action:dlq", msg,
                    )
                except Exception:
                    logger.exception("Failed to dead-letter CWS allowlist action")
            finally:
                await _release_cws_lock(redis_client, holder)

        except Exception:
            logger.exception("Error in CWS allowlist action loop")
            await asyncio.sleep(5)


async def _run_exception_reconcile(
    db_pool: asyncpg.Pool,
    redis_client: "aioredis.Redis",
    config: LifecycleConfig,
    current: set[str],
) -> None:
    """Re-enable any current exception member whose OneDrive drifted to Disabled.

    The delta path only reacts to *membership* changes, so an existing member
    whose OneDrive is externally re-disabled (e.g. by a drift-remediation sweep
    that ran before exception-matching existed) never gets re-enabled. This runs
    the delta script's idempotent 'add' path against the *full* current
    membership: OneDrives already NotDisabled record 'already_compliant', while
    any found Disabled are re-enabled. The snapshot is left untouched — these
    UPNs are already tracked; only the live OneDrive state is being repaired.
    """
    if not current:
        return

    holder = f"exception_reconcile:{int(time.time())}"
    got_lock = await _acquire_cws_lock(redis_client, holder)
    if not got_lock:
        logger.info(
            "CWS lock held; deferring exception-group reconcile to next tick",
        )
        return

    dry_run = config.cws_exception_group_sync_dry_run
    scan_id: int | None = None
    jobs_path: Path | None = None
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO company_wide_sharing_scans
                    (trigger_type, triggered_by, status, dry_run,
                     exception_group_member_count)
                VALUES ('exception_group_reconcile', 'lifecycle-cron',
                        'pending', $1, $2)
                RETURNING id
                """,
                dry_run, len(current),
            )
        scan_id = row["id"]

        jobs_path = Path(f"/tmp/cws_exception_reconcile_{scan_id}.json")
        jobs_path.write_text(
            json.dumps({"adds": sorted(current), "removes": []})
        )

        logger.info(
            "CWS exception reconcile starting (scan_id=%s, members=%d, dry_run=%s)",
            scan_id, len(current), dry_run,
        )
        ps_rc, _stdout, _stderr = await _run_pwsh_script(
            "Invoke-CwsExceptionMembershipDelta.ps1",
            [
                "-JobsFile", str(jobs_path),
                "-ScanId", str(scan_id),
                "-DryRun", "1" if dry_run else "0",
            ],
            "cws-exception-reconcile",
        )
        if ps_rc != 0:
            logger.warning(
                "CWS exception reconcile script exited rc=%d for scan_id=%s",
                ps_rc, scan_id,
            )
    finally:
        if jobs_path is not None:
            try:
                jobs_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to remove jobs file %s", jobs_path)
        await _release_cws_lock(redis_client, holder)


async def cws_exception_group_sync_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Short-interval poll of the CWS exception group; fires PS only on diff.

    Every ``cws_exception_group_sync_interval_minutes`` (default 10):
      1. Fetch current Graph members of COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID.
      2. Diff against `cws_exception_group_snapshot`.
      3. Empty snapshot → bootstrap (insert all UPNs, no remediation).
      4. No diff → refresh last_seen_at, sleep.
      5. Diff → allocate scan row (trigger_type='exception_group_delta'), write
         the {adds, removes} jobs JSON to /tmp, run
         Invoke-CwsExceptionMembershipDelta.ps1, and update the snapshot ONLY
         on a successful live (non-dry-run) execution. Failed/dry-run cycles
         keep the snapshot unchanged so the next tick re-fires.
    """
    from .graph_api import get_group_member_upns

    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "cws_exception_sync"))

    interval_seconds = max(60, config.cws_exception_group_sync_interval_minutes * 60)
    reconcile_interval_seconds = (
        config.cws_exception_reconcile_interval_hours * 3600
    )
    group_id = config.company_wide_sharing_exception_group_id

    logger.info(
        "CWS exception group sync loop starting (interval=%dm, reconcile=%dh, "
        "dry_run=%s, group=%s)",
        config.cws_exception_group_sync_interval_minutes,
        config.cws_exception_reconcile_interval_hours,
        config.cws_exception_group_sync_dry_run,
        group_id,
    )

    while True:
        try:
            current = await get_group_member_upns(auth, group_id)

            # Keep the worker-facing exemption cache fresh on every tick,
            # regardless of whether the membership changed — the worker's
            # event-driven CWS drift detector reads this set and a stale or
            # missing set would remediate exception-group members' OneDrives.
            await _refresh_exception_upn_cache(redis_client, current)

            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_upn FROM cws_exception_group_snapshot"
                )
            snapshot = {r["user_upn"] for r in rows}

            adds = sorted(current - snapshot)
            removes = sorted(snapshot - current)
            # Empty snapshot → treat every current member as an add. The delta
            # script records already_compliant for OneDrives that are already
            # NotDisabled, and re_enables only those previously remediated to
            # Disabled. This closes the bootstrap-gap where pre-existing exempt
            # members whose OneDrives had been disabled would otherwise stay
            # disabled until manual intervention.

            if not adds and not removes:
                if current:
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE cws_exception_group_snapshot
                            SET last_seen_at = NOW()
                            WHERE user_upn = ANY($1::text[])
                            """,
                            list(current),
                        )
                logger.debug(
                    "CWS exception group: no change (%d members)", len(current),
                )
                # No membership delta — but an existing member's OneDrive may
                # have been re-disabled out-of-band. Reconcile the full current
                # membership on the configured cadence to self-heal that.
                last_reconcile = await _last_completed_at(
                    db_pool, "company_wide_sharing_scans",
                    "exception_group_reconcile",
                )
                reconcile_due = last_reconcile is None or (
                    datetime.now(timezone.utc) - last_reconcile
                ).total_seconds() >= reconcile_interval_seconds
                if reconcile_due:
                    await _run_exception_reconcile(
                        db_pool, redis_client, config, current,
                    )
                await asyncio.sleep(interval_seconds)
                continue

            logger.info(
                "CWS exception group membership changed: +%d adds, -%d removes",
                len(adds), len(removes),
            )

            holder = f"exception_sync:{int(time.time())}"
            got_lock = await _acquire_cws_lock(redis_client, holder)
            if not got_lock:
                logger.info(
                    "CWS lock held; deferring exception-group delta to next tick "
                    "(adds=%d removes=%d)", len(adds), len(removes),
                )
                await asyncio.sleep(interval_seconds)
                continue

            dry_run = config.cws_exception_group_sync_dry_run
            scan_id: int | None = None
            jobs_path: Path | None = None
            ps_rc = -1
            try:
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO company_wide_sharing_scans
                            (trigger_type, triggered_by, status, dry_run,
                             exception_group_member_count)
                        VALUES ('exception_group_delta', 'lifecycle-cron',
                                'pending', $1, $2)
                        RETURNING id
                        """,
                        dry_run, len(current),
                    )
                scan_id = row["id"]

                jobs_path = Path(f"/tmp/cws_exception_delta_{scan_id}.json")
                jobs_path.write_text(json.dumps({"adds": adds, "removes": removes}))

                ps_rc, _stdout, _stderr = await _run_pwsh_script(
                    "Invoke-CwsExceptionMembershipDelta.ps1",
                    [
                        "-JobsFile", str(jobs_path),
                        "-ScanId", str(scan_id),
                        # pwsh -File treats "$true"/"$false" as literal strings
                        # and fails the [bool] cast; numeric 1/0 binds correctly.
                        # (Script also overrides via the scan row's dry_run column.)
                        "-DryRun", "1" if dry_run else "0",
                    ],
                    "cws-exception-delta",
                )
                if ps_rc != 0:
                    logger.warning(
                        "CWS exception delta script exited rc=%d for scan_id=%s; "
                        "snapshot unchanged so next tick will retry",
                        ps_rc, scan_id,
                    )
            finally:
                if jobs_path is not None:
                    try:
                        jobs_path.unlink(missing_ok=True)
                    except Exception:
                        logger.warning(
                            "Failed to remove jobs file %s", jobs_path,
                        )
                await _release_cws_lock(redis_client, holder)

            # Update snapshot only on a clean live remediation. Dry-runs and
            # failed runs leave the snapshot intact so the diff re-fires.
            if ps_rc == 0 and not dry_run:
                async with db_pool.acquire() as conn:
                    async with conn.transaction():
                        if removes:
                            await conn.execute(
                                "DELETE FROM cws_exception_group_snapshot "
                                "WHERE user_upn = ANY($1::text[])",
                                removes,
                            )
                        if adds:
                            await conn.executemany(
                                """
                                INSERT INTO cws_exception_group_snapshot (user_upn)
                                VALUES ($1)
                                ON CONFLICT (user_upn) DO UPDATE
                                    SET last_seen_at = NOW()
                                """,
                                [(u,) for u in adds],
                            )
                        if current:
                            await conn.execute(
                                "UPDATE cws_exception_group_snapshot "
                                "SET last_seen_at = NOW() "
                                "WHERE user_upn = ANY($1::text[])",
                                list(current),
                            )
                logger.info(
                    "CWS exception group snapshot updated (scan_id=%s, "
                    "now tracking %d members)", scan_id, len(current),
                )
            else:
                logger.info(
                    "CWS exception group snapshot left unchanged "
                    "(dry_run=%s, rc=%d) — diff will re-fire next tick",
                    dry_run, ps_rc,
                )

        except Exception:
            logger.exception("Error in CWS exception group sync loop")

        await asyncio.sleep(interval_seconds)


async def cws_failure_diagnostic_loop(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> None:
    """Diagnose CWS remediation failures via Diagnose-OneDriveSite.ps1.

    Two trigger paths:
      - Manual: dashboard pushes a {site_url} onto
        ``sharesentinel:cws_failure_diagnostic_trigger`` when a user clicks
        "Re-diagnose" on a failed row.
      - Sweep: every ``cws_failure_diagnostic_interval_minutes`` we pick up to
        ``cws_failure_diagnostic_max_per_sweep`` rows from cws_current_failures
        that either (a) have no cws_failure_diagnostics row yet, (b) point at a
        newer failure event than the last diagnostic, or (c) whose diagnostic
        is older than ``cws_failure_diagnostic_refresh_after_hours``.

    Honours the shared CWS Redis lock so we don't compete with verification /
    drift / bulk / exception-delta PowerShell jobs for the SPO admin API.
    """
    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "cws_failure_diagnostic"))

    sweep_interval_s = max(60, config.cws_failure_diagnostic_interval_minutes * 60)
    refresh_hours = config.cws_failure_diagnostic_refresh_after_hours
    max_per_sweep = max(1, config.cws_failure_diagnostic_max_per_sweep)

    logger.info(
        "CWS failure diagnostic loop starting "
        "(sweep_interval=%dm, max_per_sweep=%d, refresh_after=%dh)",
        config.cws_failure_diagnostic_interval_minutes,
        max_per_sweep,
        refresh_hours,
    )

    last_sweep = 0.0

    async def _diagnose_one(target: dict) -> None:
        """Run the diagnostic PS script for a single site.

        The diagnostic is read-only (Get-PnPTenantSite + Graph /users) so it does
        not contend with the write-heavy remediation scripts on the SPO admin
        throttle budget. We deliberately do NOT take the shared CWS Redis lock
        here — if we did, a 6h bulk remediation would block all diagnostics for
        the duration. Pacing comes from the small inter-iteration sleep below.
        """
        site_url = target["site_url"]
        args = [
            "-SiteUrl", site_url,
            "-UserPrincipalName", target.get("user_upn") or "",
            "-SiteDisplayName", target.get("site_display_name") or "",
            "-SiteType", target.get("site_type") or "",
            "-SourceEventId", str(target.get("source_event_id") or 0),
            "-SourceErrorMessage", target.get("error_message") or "",
        ]
        await _run_pwsh_script(
            "Invoke-CwsFailureDiagnostic.ps1",
            args,
            "cws-diagnostic",
        )

    while True:
        try:
            # 1. Drain manual triggers (highest priority).
            msg = await redis_client.lpop("sharesentinel:cws_failure_diagnostic_trigger")
            if msg:
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    preview = msg[:200] if isinstance(msg, (bytes, bytearray)) else str(msg)[:200]
                    logger.warning("Discarding malformed diagnostic trigger: %r", preview)
                    continue
                site_url = data.get("site_url", "")
                if not site_url.startswith("https://"):
                    logger.warning("Rejected invalid site_url: %s", site_url[:80])
                    continue
                # Look up the latest failure context for this site.
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT event_id AS source_event_id, site_url, user_upn,
                               site_display_name, site_type, error_message
                        FROM cws_current_failures
                        WHERE site_url = $1
                        """,
                        site_url,
                    )
                if not row:
                    logger.info(
                        "No current failure for %s — skipping diagnostic", site_url,
                    )
                    continue
                logger.info("Manual CWS diagnostic for %s", site_url)
                await _diagnose_one({**dict(row), "source": "manual"})
                continue

            # 2. Scheduled sweep.
            now = time.time()
            if now - last_sweep < sweep_interval_s:
                await asyncio.sleep(5)
                continue
            last_sweep = now

            async with db_pool.acquire() as conn:
                targets = await conn.fetch(
                    """
                    SELECT f.event_id AS source_event_id, f.site_url, f.user_upn,
                           f.site_display_name, f.site_type, f.error_message
                    FROM cws_current_failures f
                    LEFT JOIN cws_failure_diagnostics d ON d.site_url = f.site_url
                    WHERE d.site_url IS NULL
                       OR d.last_diagnosed_at < NOW() - make_interval(hours => $1)
                       OR d.source_event_id IS DISTINCT FROM f.event_id
                       -- Re-try transient classifier outcomes immediately so a
                       -- mid-deploy mount blip doesn't stick around for 72h.
                       OR d.failure_category IN ('diagnostic_unavailable', 'diagnostic_error')
                    ORDER BY f.failed_at DESC
                    LIMIT $2
                    """,
                    refresh_hours, max_per_sweep,
                )

            if not targets:
                logger.debug("CWS diagnostic sweep: nothing to diagnose")
                continue

            logger.info(
                "CWS diagnostic sweep: %d site(s) need diagnosis", len(targets),
            )
            for row in targets:
                try:
                    await _diagnose_one({**dict(row), "source": "sweep"})
                except Exception:
                    logger.exception(
                        "Failed to diagnose %s", row["site_url"],
                    )
                # Be a polite neighbour to the SPO admin API — diagnostics are
                # bursty (one PnP connect per site). 2s between targets keeps
                # us well under any reasonable throttle limit and below the
                # write-side scripts' typical batch cadence.
                await asyncio.sleep(2)

        except Exception:
            logger.exception("Error in CWS failure diagnostic loop")
            await asyncio.sleep(30)


async def folder_rescan_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Run the folder rescan processor on a fixed interval."""
    from .folder_rescan import run_folder_rescan

    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "folder_rescan"))

    interval_seconds = config.folder_rescan_interval_hours * 3600

    logger.info(
        "Folder rescan loop starting (interval=%dh, batch=%d)",
        config.folder_rescan_interval_hours,
        config.folder_rescan_batch_size,
    )

    while True:
        try:
            stats = await run_folder_rescan(db_pool, auth, redis_client, config)
            logger.info("Folder rescan cycle complete: %s", stats)
        except Exception:
            logger.exception("Error in folder rescan cycle")
        logger.info("Folder rescan sleeping %d seconds until next cycle", interval_seconds)
        await asyncio.sleep(interval_seconds)


async def audit_poll_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Run the audit log poller on a fixed interval."""
    from .audit_poller import AuditLogPoller

    redis_client = aioredis.from_url(config.redis_url)
    asyncio.create_task(_heartbeat_task(redis_client, "audit_poller"))

    poller = AuditLogPoller(auth, redis_client, db_pool, config)
    interval_seconds = config.audit_poll_interval_minutes * 60

    logger.info("Audit log poller starting (interval=%dm)", config.audit_poll_interval_minutes)

    while True:
        try:
            stats = await poller.poll()
            logger.info("Audit poll cycle complete: %s", stats)
        except Exception as exc:
            logger.exception("Error in audit poll cycle")
            await poller.save_poll_error(str(exc))
        logger.info("Audit poller sleeping %d seconds until next cycle", interval_seconds)
        await asyncio.sleep(interval_seconds)


def _cws_prereqs_ok(config: LifecycleConfig, feature: str) -> bool:
    if not config.redis_url:
        logger.error("%s enabled but REDIS_URL is not set — skipping", feature)
        return False
    if not config.sharepoint_admin_url:
        logger.error("%s enabled but SHAREPOINT_ADMIN_URL is not set — skipping", feature)
        return False
    if not config.company_wide_sharing_exception_group_id:
        logger.error(
            "%s enabled but COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID is not set — skipping",
            feature,
        )
        return False
    return True


async def _clear_stale_cws_lock(config: LifecycleConfig) -> None:
    """Clear the CWS remediation Redis lock on startup.

    The lock has a 6h TTL so that long-running scans don't lose it mid-flight,
    but if the container is killed during a scan the lock outlives the worker
    that owned it. Clearing on startup is safe because we're single-instance
    and no PowerShell subprocess can survive a container restart.
    """
    if not config.redis_url:
        return
    try:
        redis_client = aioredis.from_url(config.redis_url)
        prev = await redis_client.get(CWS_REMEDIATION_LOCK_KEY)
        if prev:
            await redis_client.delete(CWS_REMEDIATION_LOCK_KEY)
            holder = prev.decode() if isinstance(prev, bytes) else prev
            logger.warning(
                "Cleared stale CWS remediation lock from previous run (was held by %s)",
                holder,
            )
        await redis_client.aclose()
    except Exception:
        logger.warning("Failed to clear stale CWS lock on startup", exc_info=True)


async def main() -> None:
    config = LifecycleConfig.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logger.info("Lifecycle cron starting (interval=%dh)", config.check_interval_hours)

    await _clear_stale_cws_lock(config)

    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    logger.info("Database pool created")

    from .db_config import load_db_overrides
    db_overrides = await load_db_overrides(db_pool)
    if db_overrides:
        logger.info("Loaded %d DB config overrides", len(db_overrides))
        config = LifecycleConfig.from_env(db_overrides=db_overrides)

    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )

    tasks: list = [lifecycle_loop(db_pool, auth, config)]

    if config.audit_poll_enabled:
        if not config.redis_url:
            logger.error("AUDIT_POLL_ENABLED=true but REDIS_URL is not set — skipping audit poller")
        else:
            logger.info("Audit log polling enabled")
            tasks.append(audit_poll_loop(db_pool, auth, config))
    else:
        logger.info("Audit log polling disabled")

    if config.folder_rescan_enabled:
        if not config.redis_url:
            logger.error(
                "FOLDER_RESCAN_ENABLED=true but REDIS_URL is not set — "
                "skipping folder rescan"
            )
        else:
            logger.info("Folder rescan enabled (interval=%dh)", config.folder_rescan_interval_hours)
            tasks.append(folder_rescan_loop(db_pool, auth, config))
    else:
        logger.info("Folder rescan disabled")

    if config.site_policy_enabled:
        if not config.redis_url:
            logger.error(
                "SITE_POLICY_ENABLED=true but REDIS_URL is not set — "
                "skipping site policy enforcement"
            )
        elif not config.sharepoint_admin_url:
            logger.error(
                "SITE_POLICY_ENABLED=true but SHAREPOINT_ADMIN_URL is not set — "
                "skipping site policy enforcement"
            )
        else:
            logger.info("Site policy enforcement enabled")
            tasks.append(site_policy_loop(db_pool, auth, config))
    else:
        logger.info("Site policy enforcement disabled")

    if config.cws_verification_enabled and _cws_prereqs_ok(config, "CWS_VERIFICATION"):
        logger.info(
            "CWS verification sweep enabled (PowerShell, interval=%dh)",
            config.cws_verification_interval_hours,
        )
        tasks.append(cws_verification_loop(db_pool, config))
    else:
        logger.info("CWS verification sweep disabled")

    if config.cws_drift_remediation_enabled and _cws_prereqs_ok(config, "CWS_DRIFT_REMEDIATION"):
        logger.info(
            "CWS drift remediation enabled (PowerShell, interval=%dh, dry_run=%s)",
            config.cws_drift_remediation_interval_hours,
            config.cws_drift_remediation_dry_run,
        )
        tasks.append(cws_drift_remediation_loop(db_pool, config))
    else:
        logger.info("CWS drift remediation disabled")

    if config.cws_bulk_remediation_enabled and _cws_prereqs_ok(config, "CWS_BULK_REMEDIATION"):
        logger.info(
            "CWS bulk remediation enabled (PowerShell, interval=%dh, dry_run=%s)",
            config.cws_bulk_remediation_interval_hours,
            config.cws_bulk_remediation_dry_run,
        )
        tasks.append(cws_bulk_remediation_loop(db_pool, config))
    else:
        logger.info("CWS bulk remediation disabled")

    if config.cws_allowlist_action_enabled and _cws_prereqs_ok(
        config, "CWS_ALLOWLIST_ACTION"
    ):
        logger.info("CWS allowlist action consumer enabled")
        tasks.append(cws_allowlist_action_loop(db_pool, config))
    else:
        logger.info("CWS allowlist action consumer disabled")

    if config.cws_exception_group_sync_enabled and _cws_prereqs_ok(
        config, "CWS_EXCEPTION_GROUP_SYNC"
    ):
        logger.info(
            "CWS exception group sync enabled (interval=%dm, dry_run=%s)",
            config.cws_exception_group_sync_interval_minutes,
            config.cws_exception_group_sync_dry_run,
        )
        tasks.append(cws_exception_group_sync_loop(db_pool, auth, config))
    else:
        logger.info("CWS exception group sync disabled")

    if config.cws_failure_diagnostic_enabled:
        if not config.redis_url:
            logger.error(
                "CWS_FAILURE_DIAGNOSTIC_ENABLED=true but REDIS_URL is not set — "
                "skipping failure diagnostic"
            )
        else:
            logger.info(
                "CWS failure diagnostic enabled (interval=%dm)",
                config.cws_failure_diagnostic_interval_minutes,
            )
            tasks.append(cws_failure_diagnostic_loop(db_pool, config))
    else:
        logger.info("CWS failure diagnostic disabled")

    try:
        await asyncio.gather(*tasks)
    finally:
        await db_pool.close()
        logger.info("Lifecycle cron shut down")


if __name__ == "__main__":
    asyncio.run(main())
