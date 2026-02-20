"""Reprocess all audio/video events through the new transcription pipeline.

Resets A/V events to 'queued' status and pushes them back onto the Redis
job queue.  By default only events without a transcript_source are reprocessed.
Use ``--all`` to include already-transcribed events (e.g. after a pipeline
upgrade that changes analysis modes).

Usage:
    docker exec sharesentinel-worker python /app/scripts/reprocess_av_events.py --dry-run
    docker exec sharesentinel-worker python /app/scripts/reprocess_av_events.py --all --dry-run
    docker exec sharesentinel-worker python /app/scripts/reprocess_av_events.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

sys.path.insert(0, "/app")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

AV_EXTENSIONS_PATTERN = r"\.(mp4|mov|avi|mkv|wmv|flv|webm|m4v|mp3|wav|m4a|aac|flac|ogg|wma)$"

# SQL to find A/V events that need reprocessing (unprocessed only)
FIND_AV_EVENTS_UNPROCESSED = f"""
    SELECT event_id, operation, user_id, object_id, item_type,
           sharing_type, sharing_scope, sharing_permission, event_time,
           file_name, confirmed_file_name, file_size_bytes, transcript_source,
           site_url, relative_path, workload
    FROM events
    WHERE (confirmed_file_name ~* '{AV_EXTENSIONS_PATTERN}'
        OR file_name ~* '{AV_EXTENSIONS_PATTERN}')
      AND transcript_source IS NULL
      AND status IN ('completed', 'failed')
    ORDER BY
        CASE WHEN file_name ILIKE '%Meeting Recording%'
              OR confirmed_file_name ILIKE '%Meeting Recording%'
             THEN 0 ELSE 1 END,
        COALESCE(file_size_bytes, 0) ASC
"""

# SQL to find ALL A/V events for reprocessing (including already transcribed)
FIND_AV_EVENTS_ALL = f"""
    SELECT event_id, operation, user_id, object_id, item_type,
           sharing_type, sharing_scope, sharing_permission, event_time,
           file_name, confirmed_file_name, file_size_bytes, transcript_source,
           site_url, relative_path, workload
    FROM events
    WHERE (confirmed_file_name ~* '{AV_EXTENSIONS_PATTERN}'
        OR file_name ~* '{AV_EXTENSIONS_PATTERN}')
      AND status IN ('completed', 'failed', 'queued')
    ORDER BY
        CASE WHEN file_name ILIKE '%Meeting Recording%'
              OR confirmed_file_name ILIKE '%Meeting Recording%'
             THEN 0 ELSE 1 END,
        COALESCE(file_size_bytes, 0) ASC
"""

# Reset an event for reprocessing (clear verdict/analysis state, keep metadata)
RESET_EVENT = """
    UPDATE events
    SET status              = 'queued',
        processing_started_at  = NULL,
        processing_completed_at = NULL,
        transcript_source   = NULL,
        media_duration_seconds = NULL,
        extraction_method   = NULL,
        was_sampled         = false,
        sampling_description = NULL,
        file_hash           = NULL,
        hash_match_reuse    = false,
        hash_match_event_id = NULL,
        file_category       = NULL,
        failure_reason      = NULL,
        retry_count         = 0,
        temp_file_deleted   = false
    WHERE event_id = $1
"""

DELETE_VERDICT = "DELETE FROM verdicts WHERE event_id = $1"
DELETE_AUDIT = "DELETE FROM audit_log WHERE event_id = $1"


def _build_job(row: asyncpg.Record) -> dict:
    """Build a Redis queue job dict from a DB row."""
    job = {
        "event_id": row["event_id"],
        "user_id": row["user_id"] or "",
        "operation": row["operation"] or "CompanySharingLinkCreated",
        "object_id": row["object_id"] or "",
        "item_type": row["item_type"] or "file",
    }
    # Include file_name so the pipeline can use it for Teams recording detection
    # and for Stream caption retrieval (site_url needed for SP-scoped token)
    file_name = row["confirmed_file_name"] or row["file_name"] or ""
    if file_name:
        job["file_name"] = file_name
    if row["site_url"]:
        job["site_url"] = row["site_url"]
    if row.get("relative_path"):
        job["relative_path"] = row["relative_path"]
    if row.get("workload"):
        job["workload"] = row["workload"]
    if row["sharing_type"]:
        job["sharing_type"] = row["sharing_type"]
    if row["sharing_scope"]:
        job["sharing_scope"] = row["sharing_scope"]
    if row["sharing_permission"]:
        job["sharing_permission"] = row["sharing_permission"]
    if row["event_time"]:
        job["event_time"] = row["event_time"].isoformat()
    return job


async def main(dry_run: bool = True, batch_size: int = 50, include_all: bool = False) -> None:
    config = Config.from_env()

    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    redis_conn = aioredis.from_url(config.redis_url)

    try:
        # Find A/V events needing reprocessing
        query = FIND_AV_EVENTS_ALL if include_all else FIND_AV_EVENTS_UNPROCESSED
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query)

        teams_count = sum(
            1 for r in rows
            if "Meeting Recording" in (r["file_name"] or r["confirmed_file_name"] or "")
        )
        other_count = len(rows) - teams_count

        logger.info(
            "Found %d A/V events to reprocess (%d Teams recordings, %d other)",
            len(rows), teams_count, other_count,
        )

        if dry_run:
            logger.info("DRY RUN — no changes will be made")
            # Show a sample
            for r in rows[:10]:
                name = r["confirmed_file_name"] or r["file_name"] or "?"
                size = r["file_size_bytes"] or 0
                is_teams = "Meeting Recording" in name
                src = r["transcript_source"] or "none"
                has_site = "yes" if r["site_url"] else "no"
                logger.info(
                    "  [%s] %s (%s, src=%s, site_url=%s, %s)",
                    "TEAMS" if is_teams else "OTHER",
                    name[:60],
                    f"{size / 1_048_576:.1f}MB",
                    src,
                    has_site,
                    r["event_id"][:16] + "...",
                )
            if len(rows) > 10:
                logger.info("  ... and %d more", len(rows) - 10)
            return

        # Process in batches
        enqueued = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    for row in batch:
                        eid = row["event_id"]
                        # Delete old verdict and audit entries
                        await conn.execute(DELETE_VERDICT, eid)
                        await conn.execute(DELETE_AUDIT, eid)
                        # Reset event status
                        await conn.execute(RESET_EVENT, eid)

            # Enqueue jobs on Redis
            pipe = redis_conn.pipeline()
            for row in batch:
                job = _build_job(row)
                pipe.rpush("sharesentinel:jobs", json.dumps(job))
            await pipe.execute()

            enqueued += len(batch)
            logger.info("Batch %d: reset and enqueued %d events (%d/%d total)",
                        i // batch_size + 1, len(batch), enqueued, len(rows))

        logger.info("Done! Enqueued %d A/V events for reprocessing", enqueued)

    finally:
        await db_pool.close()
        await redis_conn.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reprocess A/V events through transcription pipeline")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Show what would be reprocessed without making changes")
    parser.add_argument("--all", action="store_true", default=False,
                        help="Include already-transcribed events (reprocess everything)")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Number of events per batch (default: 50)")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, batch_size=args.batch_size, include_all=args.all))
