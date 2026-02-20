#!/usr/bin/env python3
"""Re-queue Loop events for format conversion reprocessing.

These events were previously parked as pending_manual_inspection and can now
be processed automatically via Graph API server-side format conversion
(Loop → HTML via ?format=html).

Note: Whiteboards are NOT re-queued — Graph API format conversion returns 500
for whiteboards, and the raw .whiteboard binary is an opaque Fluid Framework
container with no extractable text.

Usage:
    docker exec sharesentinel-worker python scripts/requeue_convertible_events.py --dry-run
    docker exec sharesentinel-worker python scripts/requeue_convertible_events.py --execute
    docker exec sharesentinel-worker python scripts/requeue_convertible_events.py --execute --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime

# Add the worker app to the path (container WORKDIR is /app)
sys.path.insert(0, "/app")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

QUEUE_KEY = "sharesentinel:jobs"

FIND_CONVERTIBLE_EVENTS = """
    SELECT event_id, file_name, content_type, object_id, user_id,
           item_type, operation, workload, site_url, relative_path,
           sharing_type, sharing_scope, sharing_permission, event_time
    FROM events
    WHERE status = 'pending_manual_inspection'
      AND content_type = 'loop'
    ORDER BY received_at
    LIMIT $1
"""

RESET_EVENT_STATUS = """
    UPDATE events
    SET status = 'queued',
        processing_started_at  = NULL,
        processing_completed_at = NULL,
        extraction_method   = NULL,
        was_sampled         = false,
        sampling_description = NULL,
        file_hash           = NULL,
        hash_match_reuse    = false,
        hash_match_event_id = NULL,
        file_category       = NULL,
        failure_reason      = NULL,
        retry_count         = 0,
        temp_file_deleted   = false,
        updated_at          = NOW()
    WHERE event_id = $1
"""

DELETE_VERDICT = "DELETE FROM verdicts WHERE event_id = $1"
DELETE_AUDIT = "DELETE FROM audit_log WHERE event_id = $1"


def _build_job(row: asyncpg.Record) -> dict:
    """Build a Redis queue job dict from a database row."""
    job: dict[str, str | None] = {
        "event_id": row["event_id"],
        "object_id": row["object_id"] or "",
        "user_id": row["user_id"] or "",
        "item_type": row["item_type"] or "File",
        "operation": row["operation"] or "",
        "workload": row["workload"] or "",
        "file_name": row["file_name"] or "",
        "site_url": row["site_url"] or "",
        "relative_path": row["relative_path"] or "",
        "sharing_type": row["sharing_type"] or "",
        "sharing_scope": row["sharing_scope"] or "",
        "sharing_permission": row["sharing_permission"] or "",
    }

    # Serialize event_time as ISO string; handle None gracefully
    event_time = row["event_time"]
    if isinstance(event_time, datetime):
        job["event_time"] = event_time.isoformat()
    elif event_time is not None:
        job["event_time"] = str(event_time)
    else:
        job["event_time"] = ""

    return job


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-queue Loop/Whiteboard events for format conversion reprocessing.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show what would be re-queued without making changes (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually re-queue the events",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max number of events to re-queue (default: 100)",
    )
    args = parser.parse_args()

    # --execute flips dry_run off
    dry_run = not args.execute

    config = Config.from_env()
    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)

    try:
        # Find matching events
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(FIND_CONVERTIBLE_EVENTS, args.limit)

        total_found = len(rows)
        logger.info("Found %d convertible event(s) (loop/whiteboard, pending_manual_inspection)", total_found)

        if total_found == 0:
            logger.info("Nothing to do.")
            return

        if dry_run:
            logger.info("DRY RUN -- no changes will be made")
            for row in rows:
                logger.info(
                    "  [%s] event_id=%s  file=%s  user=%s  site=%s",
                    (row["content_type"] or "?").upper(),
                    row["event_id"],
                    row["file_name"] or "(no name)",
                    row["user_id"] or "(no user)",
                    row["site_url"] or "(no site)",
                )
            logger.info(
                "Summary: %d event(s) would be re-queued. Run with --execute to proceed.",
                total_found,
            )
            return

        # Execute mode: reset events, clear old verdicts/audit, push to Redis
        enqueued = 0
        errors = 0

        for row in rows:
            eid = row["event_id"]
            try:
                async with db_pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(DELETE_VERDICT, eid)
                        await conn.execute(DELETE_AUDIT, eid)
                        await conn.execute(RESET_EVENT_STATUS, eid)

                job = _build_job(row)
                await redis_conn.rpush(QUEUE_KEY, json.dumps(job))
                enqueued += 1

                logger.info(
                    "  Re-queued [%s] event_id=%s  file=%s",
                    (row["content_type"] or "?").upper(),
                    eid,
                    row["file_name"] or "(no name)",
                )
            except Exception:
                errors += 1
                logger.error("  Failed to re-queue event_id=%s", eid, exc_info=True)

        logger.info(
            "Summary: %d found, %d re-queued, %d errors",
            total_found, enqueued, errors,
        )

    finally:
        await db_pool.close()
        await redis_conn.aclose()


if __name__ == "__main__":
    asyncio.run(main())
