"""Re-enqueue all folder events for reprocessing with folder enumeration.

Usage (inside worker container):
    python -m scripts.reprocess_folders [--limit N] [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import os
import sys

import asyncpg
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QUEUE_KEY = "sharesentinel:jobs"


async def main(limit: int | None, dry_run: bool):
    db_url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

    pool = await asyncpg.create_pool(db_url)

    # Fetch folder events that were previously completed with the old logic
    query = """
        SELECT event_id, operation, workload, user_id, object_id,
               site_url, file_name, relative_path, item_type,
               sharing_type, sharing_scope, sharing_permission,
               event_time, raw_payload
        FROM events
        WHERE item_type = 'Folder'
          AND status = 'completed'
          AND folder_total_children IS NULL
        ORDER BY received_at DESC
    """
    if limit:
        query += f" LIMIT {limit}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query)

    logger.info("Found %d folder events to reprocess", len(rows))

    if not rows:
        return

    if dry_run:
        for r in rows:
            logger.info("  [DRY RUN] Would reprocess: %s — %s", r["event_id"], r["file_name"])
        return

    r = aioredis.from_url(redis_url)

    # Reset each event status and delete old verdict, then enqueue
    reset_count = 0
    for row in rows:
        eid = row["event_id"]

        # Always reconstruct from DB columns to ensure event_id is present
        # (raw_payload from Splunk may not contain event_id or other fields)
        job_data = {
            "event_id": eid,
            "operation": row["operation"],
            "workload": row["workload"],
            "user_id": row["user_id"],
            "object_id": row["object_id"],
            "site_url": row["site_url"],
            "file_name": row["file_name"],
            "relative_path": row["relative_path"],
            "item_type": "Folder",
            "sharing_type": row["sharing_type"],
            "sharing_scope": row["sharing_scope"],
            "sharing_permission": row["sharing_permission"],
            "event_time": row["event_time"].isoformat() if row["event_time"] else None,
        }

        # Reset the event so it can be reprocessed:
        # Delete the event row (create_event will recreate it fresh)
        # Must delete child references first due to FK constraint
        async with pool.acquire() as conn:
            # Delete any existing child events from prior runs
            await conn.execute(
                "DELETE FROM verdicts WHERE event_id IN (SELECT event_id FROM events WHERE parent_event_id = $1)", eid
            )
            await conn.execute(
                "DELETE FROM audit_log WHERE event_id IN (SELECT event_id FROM events WHERE parent_event_id = $1)", eid
            )
            await conn.execute("DELETE FROM events WHERE parent_event_id = $1", eid)
            # Delete parent verdict and audit
            await conn.execute("DELETE FROM verdicts WHERE event_id = $1", eid)
            await conn.execute("DELETE FROM audit_log WHERE event_id = $1", eid)
            # Delete the event so create_event works fresh
            await conn.execute("DELETE FROM events WHERE event_id = $1", eid)

        # Push to Redis queue
        await r.rpush(QUEUE_KEY, json.dumps(job_data))
        reset_count += 1
        logger.info("  Enqueued: %s — %s", eid, row["file_name"])

    await r.aclose()
    await pool.close()
    logger.info("Done. Enqueued %d folder events for reprocessing.", reset_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max events to reprocess")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.dry_run))
