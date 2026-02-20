"""Reprocess all events flagged as tier_1/tier_2 by a given AI provider.

Deletes existing verdicts, resets event status, and re-enqueues jobs
so the worker re-runs the full pipeline (with the current prompt and
second-look configuration).

Usage (inside worker container):
    python -m scripts.reprocess_flagged [--dry-run] [--provider openai] [--limit N]
"""

import asyncio
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reprocess_flagged")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config

QUEUE_KEY = "sharesentinel:jobs"


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    provider_filter = "openai"
    limit = None

    for i, arg in enumerate(sys.argv):
        if arg == "--provider" and i + 1 < len(sys.argv):
            provider_filter = sys.argv[i + 1]
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    config = Config.from_env()
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)

    # Fetch all flagged events
    query = """
        SELECT e.event_id, e.operation, e.workload, e.user_id, e.object_id,
               e.site_url, e.file_name, e.relative_path, e.item_type,
               e.sharing_type, e.sharing_scope, e.sharing_permission,
               e.event_time,
               v.id as verdict_id, v.escalation_tier, v.analysis_mode,
               v.categories_detected, v.ai_provider, v.ai_model
        FROM events e
        JOIN verdicts v ON v.event_id = e.event_id
        WHERE v.escalation_tier IN ('tier_1', 'tier_2')
          AND v.ai_provider = $1
        ORDER BY v.id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, provider_filter)

    events = [dict(r) for r in rows]
    if limit:
        events = events[:limit]

    logger.info(
        "Found %d flagged events from provider=%s (limit=%s)",
        len(events), provider_filter, limit,
    )

    if not events:
        print("No events to reprocess.")
        await pool.close()
        return

    if dry_run:
        print(f"\n{'#':<4} {'VID':<6} {'Mode':<14} {'Tier':<8} {'Categories':<30} {'File':<50}")
        print("-" * 112)
        for i, ev in enumerate(events, 1):
            cats = ev["categories_detected"]
            if isinstance(cats, str):
                cats = json.loads(cats)
            if isinstance(cats, list):
                cat_str = ",".join(str(c) for c in cats)
            else:
                cat_str = str(cats)
            print(
                f"{i:<4} {ev['verdict_id']:<6} {ev['analysis_mode']:<14} "
                f"{ev['escalation_tier']:<8} {cat_str[:29]:<30} "
                f"{(ev['file_name'] or '')[:49]:<50}"
            )
        print(f"\n[dry-run] {len(events)} events would be reprocessed. No changes made.")
        await pool.close()
        return

    # Delete all related records in a transaction so re-enqueue doesn't hit dedup
    event_ids = [ev["event_id"] for ev in events]
    verdict_ids = [ev["verdict_id"] for ev in events]

    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted_audit = await conn.execute(
                "DELETE FROM audit_log WHERE event_id = ANY($1::text[])",
                event_ids,
            )
            logger.info("Deleted audit log entries: %s", deleted_audit)

            deleted_hashes = await conn.execute(
                "DELETE FROM file_hashes WHERE first_event_id = ANY($1::text[])",
                event_ids,
            )
            logger.info("Deleted file hash entries: %s", deleted_hashes)

            deleted_remediations = await conn.execute(
                "DELETE FROM remediations WHERE event_id = ANY($1::text[])",
                event_ids,
            )
            logger.info("Deleted remediations: %s", deleted_remediations)

            deleted_verdicts = await conn.execute(
                "DELETE FROM verdicts WHERE event_id = ANY($1::text[])",
                event_ids,
            )
            logger.info("Deleted verdicts: %s", deleted_verdicts)

            deleted_events = await conn.execute(
                "DELETE FROM events WHERE event_id = ANY($1::text[])",
                event_ids,
            )
            logger.info("Deleted events: %s", deleted_events)

    # Re-enqueue jobs
    enqueued = 0
    for ev in events:
        job = {
            "event_id": ev["event_id"],
            "operation": ev["operation"] or "",
            "workload": ev["workload"] or "",
            "user_id": ev["user_id"] or "",
            "object_id": ev["object_id"] or "",
            "site_url": ev["site_url"] or "",
            "file_name": ev["file_name"] or "",
            "relative_path": ev["relative_path"] or "",
            "item_type": ev["item_type"] or "File",
            "sharing_type": ev["sharing_type"] or "",
            "sharing_scope": ev["sharing_scope"] or "",
            "sharing_permission": ev["sharing_permission"] or "",
            "event_time": ev["event_time"].isoformat() if ev["event_time"] else "",
        }
        await redis_conn.rpush(QUEUE_KEY, json.dumps(job))
        enqueued += 1

    logger.info("Enqueued %d jobs to %s", enqueued, QUEUE_KEY)
    print(f"\nReprocessing started: {enqueued} events enqueued.")
    print("Monitor progress with: docker compose logs -f worker")

    await redis_conn.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
