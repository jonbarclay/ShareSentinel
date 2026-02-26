"""Entry point for the lifecycle cron service.

Runs two concurrent loops:
1. Lifecycle processor — checks sharing link expiry milestones (daily)
2. Audit log poller — queries Graph API audit logs for new sharing events (hourly)
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

    try:
        await asyncio.gather(*tasks)
    finally:
        await db_pool.close()
        logger.info("Lifecycle cron shut down")


if __name__ == "__main__":
    asyncio.run(main())
