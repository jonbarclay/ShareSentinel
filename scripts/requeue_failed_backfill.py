"""Requeue failed access-checkpoint backfill events for another attempt.

Backfill events that hit a transient error (e.g. an httpx.ConnectError while
downloading from Graph) land in status='failed'. The normal backfill script
won't re-enqueue them because their object_id is already in the events table,
and the orchestrator's create_event() only re-claims an event whose status is
'queued'. This helper bridges that: it flips matching failed events back to
'queued' and re-pushes a reconstructed job payload to the Redis queue.

The reconstructed job carries force_analysis / skip_if_unprocessable so it
behaves identically to the original backfill job.

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.requeue_failed_backfill --dry-run
    docker exec sharesentinel-worker python -m scripts.requeue_failed_backfill
    docker exec sharesentinel-worker python -m scripts.requeue_failed_backfill \
        --operation AccessCheckpointBackfill
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("requeue_failed_backfill")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config

QUEUE_KEY = "sharesentinel:jobs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Requeue failed backfill events for another attempt",
    )
    parser.add_argument(
        "--operation",
        default="AccessCheckpointBackfill",
        help="Only requeue events with this operation (default: AccessCheckpointBackfill)",
    )
    parser.add_argument(
        "--failure-reason",
        default="",
        help="Optional: only requeue events with this failure_reason",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be requeued without changing anything")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max events to requeue, 0=unlimited (default: 0)")
    return parser.parse_args()


def build_job_from_row(row: dict) -> dict:
    """Reconstruct a queue job payload from a stored events row."""
    raw_payload = row.get("raw_payload")
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except (ValueError, TypeError):
            raw_payload = {"source": "requeue_failed_backfill"}
    elif raw_payload is None:
        raw_payload = {"source": "requeue_failed_backfill"}

    event_time = row.get("event_time")
    return {
        "event_id": row["event_id"],
        "operation": row.get("operation", ""),
        "workload": row.get("workload"),
        "user_id": row.get("user_id", "") or "",
        "object_id": row.get("object_id", ""),
        "site_url": row.get("site_url"),
        "file_name": row.get("file_name"),
        "relative_path": row.get("relative_path"),
        "item_type": row.get("item_type", "File") or "File",
        "sharing_type": row.get("sharing_type"),
        "sharing_scope": row.get("sharing_scope"),
        "sharing_permission": row.get("sharing_permission"),
        "event_time": event_time.isoformat() if event_time else None,
        "received_at": datetime.now(timezone.utc).isoformat(),
        # Preserve backfill pipeline behaviour.
        "force_analysis": True,
        "skip_if_unprocessable": True,
        "raw_payload": raw_payload,
    }


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)

    where = ["status = 'failed'", "operation = $1"]
    params: list = [args.operation]
    if args.failure_reason:
        where.append(f"failure_reason = ${len(params) + 1}")
        params.append(args.failure_reason)

    query = (
        "SELECT event_id, operation, workload, user_id, object_id, site_url, "
        "file_name, relative_path, item_type, sharing_type, sharing_scope, "
        "sharing_permission, event_time, raw_payload, failure_reason "
        f"FROM events WHERE {' AND '.join(where)} ORDER BY updated_at"
    )
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(query, *params)]

    if args.limit > 0:
        rows = rows[: args.limit]

    print()
    print("=" * 60)
    print("  Requeue Failed Backfill Events")
    print("=" * 60)
    print(f"  Operation filter:        {args.operation}")
    if args.failure_reason:
        print(f"  Failure-reason filter:   {args.failure_reason}")
    print(f"  Failed events matched:   {len(rows)}")
    print("=" * 60)

    if not rows:
        print("\nNothing to requeue.")
        await pool.close()
        return

    for r in rows:
        print(f"  · {r['event_id'][:16]}…  {(r.get('file_name') or '')[:60]}")

    if args.dry_run:
        print(f"\n[dry-run] Would requeue {len(rows)} events. No changes made.")
        await pool.close()
        return

    # Flip to 'queued' first so the worker's create_event() upsert (which only
    # re-claims status='queued' rows) will pick them up, then push to Redis.
    event_ids = [r["event_id"] for r in rows]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET status = 'queued', failure_reason = NULL, "
            "updated_at = now() WHERE event_id = ANY($1::text[])",
            event_ids,
        )
    await pool.close()

    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)
    pipe = redis_conn.pipeline()
    for r in rows:
        pipe.rpush(QUEUE_KEY, json.dumps(build_job_from_row(r)))
    await pipe.execute()
    queue_depth = await redis_conn.llen(QUEUE_KEY)
    await redis_conn.aclose()

    print(f"\nRequeued {len(rows)} events (status→queued, re-pushed to {QUEUE_KEY}).")
    print(f"  Queue depth: {queue_depth}")
    print("\nMonitor with: docker compose logs -f worker")


if __name__ == "__main__":
    asyncio.run(main())
