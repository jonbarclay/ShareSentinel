"""Entry point for the lifecycle cron service.

Runs up to three concurrent loops:
1. Lifecycle processor — checks sharing link expiry milestones (daily)
2. Audit log poller — queries Graph API audit logs for new sharing events (every 15m)
3. Allowlist enforcer — enforces anonymous sharing policy on SharePoint sites (weekly + manual)
"""

from __future__ import annotations

import asyncio
import logging
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


async def allowlist_enforcement_loop(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> None:
    """Check for manual triggers every 60s, run weekly scheduled enforcement."""
    import json

    import redis.asyncio as aioredis

    from .allowlist_enforcer import run_enforcement

    redis_client = aioredis.from_url(config.redis_url)
    interval_seconds = config.allowlist_enforcement_interval_hours * 3600

    logger.info(
        "Allowlist enforcement loop starting (interval=%dh)",
        config.allowlist_enforcement_interval_hours,
    )

    while True:
        try:
            # 1. Check Redis for manual trigger
            trigger = await redis_client.lpop("sharesentinel:allowlist_sync_trigger")
            if trigger:
                data = json.loads(trigger)
                sync_id = data["sync_id"]
                logger.info("Manual allowlist sync triggered (sync_id=%d)", sync_id)
                await run_enforcement(
                    db_pool, config, sync_id=sync_id, full_enforcement=False,
                )
                continue  # Check for more triggers before sleeping

            # 2. Check if scheduled run is due
            async with db_pool.acquire() as conn:
                last_scheduled = await conn.fetchrow(
                    """
                    SELECT completed_at FROM site_allowlist_syncs
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
                        INSERT INTO site_allowlist_syncs
                            (trigger_type, status)
                        VALUES ('scheduled', 'pending')
                        RETURNING id
                        """
                    )
                sync_id = row["id"]
                logger.info("Scheduled allowlist enforcement starting (sync_id=%d)", sync_id)
                await run_enforcement(
                    db_pool, config, sync_id=sync_id, full_enforcement=True,
                )

        except Exception:
            logger.exception("Error in allowlist enforcement loop")

        await asyncio.sleep(60)  # Poll every 60s for manual triggers


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

    # Set up Graph API auth
    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )

    tasks = [lifecycle_loop(db_pool, auth, config)]

    if config.audit_poll_enabled:
        if not config.redis_url:
            logger.error("AUDIT_POLL_ENABLED=true but REDIS_URL is not set — skipping audit poller")
        else:
            logger.info("Audit log polling enabled")
            tasks.append(audit_poll_loop(db_pool, auth, config))
    else:
        logger.info("Audit log polling disabled")

    if config.allowlist_enforcement_enabled:
        if not config.redis_url:
            logger.error(
                "ALLOWLIST_ENFORCEMENT_ENABLED=true but REDIS_URL is not set — "
                "skipping allowlist enforcement"
            )
        elif not config.sharepoint_admin_url:
            logger.error(
                "ALLOWLIST_ENFORCEMENT_ENABLED=true but SHAREPOINT_ADMIN_URL is not set — "
                "skipping allowlist enforcement"
            )
        else:
            logger.info("Allowlist enforcement enabled")
            tasks.append(allowlist_enforcement_loop(db_pool, config))
    else:
        logger.info("Allowlist enforcement disabled")

    try:
        await asyncio.gather(*tasks)
    finally:
        await db_pool.close()
        logger.info("Lifecycle cron shut down")


if __name__ == "__main__":
    asyncio.run(main())
