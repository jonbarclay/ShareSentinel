"""Entry point for the lifecycle cron service.

Runs up to four concurrent loops:
1. Lifecycle processor — checks sharing link expiry milestones (daily)
2. Audit log poller — queries Graph API audit logs for new sharing events (every 15m)
3. Site policy enforcer — enforces visibility + sharing policies on SharePoint sites (daily + manual)
4. Folder rescan — re-checks shared folders for new/modified files (weekly)
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys

import asyncpg

from .config import LifecycleConfig
from .graph_api import GraphAuth
from .processor import process_lifecycle_milestones

logger = logging.getLogger(__name__)


async def lifecycle_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Run the lifecycle milestone processor on a fixed interval."""
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
    """Daily site policy scanner + enforcer (replaces allowlist_enforcement_loop).

    Polls Redis every 60s for manual triggers, runs scheduled scans at configured interval.
    Handles both visibility (Public->Private) and sharing (anonymous link) enforcement.
    """
    import json

    import redis.asyncio as aioredis

    from .site_policy_scanner import (
        apply_sharing_for_site,
        apply_visibility_for_group,
        revoke_sharing_for_site,
        revoke_visibility_for_group,
        run_site_policy_scan,
    )

    redis_client = aioredis.from_url(config.redis_url)
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
                action_data = json.loads(action_msg)
                action_type = action_data.get("action")

                # Validate action type
                if action_type not in _ALLOWED_ACTIONS:
                    logger.warning("Rejected unknown site policy action: %s", action_type)
                    continue

                # Validate group_id for visibility actions
                if action_type in ("set_public", "set_private"):
                    gid = action_data.get("group_id", "")
                    if not _UUID_RE.match(gid):
                        logger.warning("Rejected invalid group_id: %s", gid[:50])
                        continue

                # Validate site_url for sharing actions
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
                        db_pool, auth,
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

                continue  # Check for more actions before sleeping

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
                continue  # Check for more triggers before sleeping

            # 2. Check if scheduled run is due
            async with db_pool.acquire() as conn:
                last_scheduled = await conn.fetchrow(
                    """
                    SELECT completed_at FROM site_policy_scans
                    WHERE trigger_type = 'scheduled'
                      AND status = 'completed'
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """
                )
            run_due = True
            if last_scheduled and last_scheduled["completed_at"]:
                from datetime import datetime, timezone

                elapsed = (
                    datetime.now(timezone.utc) - last_scheduled["completed_at"]
                ).total_seconds()
                run_due = elapsed >= interval_seconds

            if run_due:
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

        except Exception:
            logger.exception("Error in site policy loop")

        await asyncio.sleep(60)  # Poll every 60s for manual triggers


async def folder_rescan_loop(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> None:
    """Run the folder rescan processor on a fixed interval."""
    import redis.asyncio as aioredis

    from .folder_rescan import run_folder_rescan

    redis_client = aioredis.from_url(config.redis_url)
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
    import redis.asyncio as aioredis

    from .audit_poller import AuditLogPoller

    redis_client = aioredis.from_url(config.redis_url)
    poller = AuditLogPoller(auth, redis_client, db_pool, config)
    interval_seconds = config.audit_poll_interval_minutes * 60

    logger.info("Audit log poller starting (interval=%dm)", config.audit_poll_interval_minutes)

    while True:
        try:
            stats = await poller.poll()
            logger.info("Audit poll cycle complete: %s", stats)
        except Exception:
            logger.exception("Error in audit poll cycle")
        logger.info("Audit poller sleeping %d seconds until next cycle", interval_seconds)
        await asyncio.sleep(interval_seconds)


async def main() -> None:
    config = LifecycleConfig.from_env()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logger.info("Lifecycle cron starting (interval=%dh)", config.check_interval_hours)

    # Connect to database
    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    logger.info("Database pool created")

    # Reload config with DB overrides from admin panel
    from .db_config import load_db_overrides
    db_overrides = await load_db_overrides(db_pool)
    if db_overrides:
        logger.info("Loaded %d DB config overrides", len(db_overrides))
        config = LifecycleConfig.from_env(db_overrides=db_overrides)

    # Set up Graph API auth
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

    try:
        await asyncio.gather(*tasks)
    finally:
        await db_pool.close()
        logger.info("Lifecycle cron shut down")


if __name__ == "__main__":
    asyncio.run(main())
