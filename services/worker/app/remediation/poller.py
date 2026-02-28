"""Background loop that polls the remediations table for pending work."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import asyncpg
import redis.asyncio as aioredis

from ..config import Config
from ..graph_api.auth import GraphAuth
from .executor import execute_remediation, _mark_failed

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 30


async def remediation_poller(
    db_pool: asyncpg.Pool,
    config: Config,
    auth: GraphAuth,
    redis_conn: Optional[aioredis.Redis] = None,
) -> None:
    """Continuously poll for pending remediations and execute them.

    Uses ``FOR UPDATE SKIP LOCKED`` to safely claim one row at a time,
    preventing double-processing if multiple workers ever exist.
    """
    logger.info("Remediation poller started (interval=%ds)", POLL_INTERVAL_S)

    while True:
        try:
            row = await _claim_next(db_pool)
            if row:
                logger.info(
                    "Claimed remediation id=%d event_id=%s",
                    row["id"], row["event_id"],
                )
                try:
                    await execute_remediation(row, db_pool, config, auth, redis_conn=redis_conn)
                except Exception:
                    logger.exception(
                        "Unhandled error executing remediation id=%d", row["id"],
                    )
                    await _mark_failed(
                        db_pool, row["id"], "Unhandled exception during execution",
                    )
        except Exception:
            logger.exception("Error in remediation poller loop")

        await asyncio.sleep(POLL_INTERVAL_S)


async def _claim_next(db_pool: asyncpg.Pool) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest pending remediation row.

    Sets status='in_progress' and started_at, returns the row dict,
    or None if nothing is pending.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE remediations
            SET status = 'in_progress',
                started_at = NOW(),
                updated_at = NOW()
            WHERE id = (
                SELECT id FROM remediations
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """
        )
        return dict(row) if row else None
