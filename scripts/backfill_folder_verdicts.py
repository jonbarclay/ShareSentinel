"""Backfill folder verdicts and requeue hex64 folder events.

Phase 1 (--fix-verdicts):
    Fix parent folder verdicts that have escalation_tier='none' even though
    their children have tier_1/tier_2 flagged verdicts.  Aggregates child
    categories into the parent verdict.

Phase 2 (--requeue-folders):
    Requeue hex64-format folder events that have 0 child rows in the database.
    These failed because event_id VARCHAR(64) couldn't store the child IDs.
    Requires migration 015_widen_event_id.sql to be applied first.

Run inside the worker container:
    python -m scripts.backfill_folder_verdicts --fix-verdicts [--dry-run]
    python -m scripts.backfill_folder_verdicts --requeue-folders [--dry-run]
    python -m scripts.backfill_folder_verdicts --fix-verdicts --requeue-folders [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import sys

import asyncpg
import redis.asyncio as aioredis

from app.ai.base_provider import compute_escalation_tier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_folder_verdicts")

QUEUE_KEY = "sharesentinel:jobs"


# ──────────────────────────────────────────────────────────────
# Phase 1: Fix parent folder verdicts
# ──────────────────────────────────────────────────────────────

async def fix_verdicts(pool: asyncpg.Pool, dry_run: bool) -> None:
    """Update parent folder verdicts whose escalation_tier is wrong."""

    rows = await pool.fetch("""
        SELECT v.id        AS verdict_id,
               v.event_id,
               e.file_name
        FROM verdicts v
        JOIN events e ON e.event_id = v.event_id
        WHERE v.analysis_mode = 'folder_enumeration'
          AND v.escalation_tier = 'none'
          AND v.categories_detected = '[]'::jsonb
          AND EXISTS (
              SELECT 1
              FROM events child
              JOIN verdicts cv ON cv.event_id = child.event_id
              WHERE child.parent_event_id = v.event_id
                AND cv.escalation_tier IN ('tier_1', 'tier_2')
          )
    """)

    logger.info("Phase 1: Found %d parent folder verdicts to fix", len(rows))

    if not rows:
        return

    if dry_run:
        for r in rows:
            logger.info(
                "  [DRY RUN] Would fix verdict id=%d event=%s (%s)",
                r["verdict_id"], r["event_id"], r["file_name"],
            )
        return

    fixed = 0
    errors = 0

    for row in rows:
        try:
            # Fetch all child verdicts for this parent
            child_verdicts = await pool.fetch("""
                SELECT cv.categories_detected,
                       cv.category_assessments,
                       cv.escalation_tier,
                       cv.affected_count,
                       cv.pii_types_found
                FROM events child
                JOIN verdicts cv ON cv.event_id = child.event_id
                WHERE child.parent_event_id = $1
                  AND cv.escalation_tier IN ('tier_1', 'tier_2')
            """, row["event_id"])

            # Aggregate categories across all flagged children
            all_category_ids: set[str] = set()
            all_assessments: list[dict] = []
            max_affected = 0
            all_pii_types: set[str] = set()

            for cv in child_verdicts:
                cats = cv["categories_detected"] or []
                if isinstance(cats, str):
                    cats = json.loads(cats)
                all_category_ids.update(cats)

                assessments = cv["category_assessments"] or []
                if isinstance(assessments, str):
                    assessments = json.loads(assessments)
                all_assessments.extend(assessments)

                max_affected = max(max_affected, cv["affected_count"] or 0)

                pii = cv["pii_types_found"] or []
                if isinstance(pii, str):
                    pii = json.loads(pii)
                all_pii_types.update(pii)

            # Compute the correct escalation tier
            new_tier = compute_escalation_tier(
                category_ids=all_category_ids,
                affected_count=max_affected,
                pii_types_found=list(all_pii_types),
            )

            # Deduplicate assessments by category id (keep first occurrence)
            seen_ids: set[str] = set()
            deduped_assessments: list[dict] = []
            for a in all_assessments:
                aid = a.get("id", "")
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    deduped_assessments.append(a)

            await pool.execute("""
                UPDATE verdicts
                SET categories_detected = $1::jsonb,
                    category_assessments = $2::jsonb,
                    escalation_tier = $3,
                    notification_required = $4
                WHERE id = $5
            """,
                json.dumps(sorted(all_category_ids)),
                json.dumps(deduped_assessments),
                new_tier,
                new_tier in ("tier_1", "tier_2"),
                row["verdict_id"],
            )

            fixed += 1
            logger.info(
                "  Fixed verdict id=%d event=%s: tier=%s categories=%s",
                row["verdict_id"], row["event_id"], new_tier,
                sorted(all_category_ids),
            )

        except Exception:
            errors += 1
            logger.warning(
                "Failed to fix verdict for event_id=%s",
                row["event_id"], exc_info=True,
            )

    logger.info(
        "Phase 1 complete: %d fixed, %d errors (of %d found)",
        fixed, errors, len(rows),
    )


# ──────────────────────────────────────────────────────────────
# Phase 2: Requeue hex64 folder events
# ──────────────────────────────────────────────────────────────

async def requeue_folders(pool: asyncpg.Pool, redis_url: str, dry_run: bool) -> None:
    """Requeue hex64-format folder events with 0 child rows."""

    rows = await pool.fetch("""
        SELECT e.event_id, e.operation, e.workload, e.user_id, e.object_id,
               e.site_url, e.file_name, e.relative_path, e.item_type,
               e.sharing_type, e.sharing_scope, e.sharing_permission,
               e.event_time, e.folder_total_children, e.folder_processed_children
        FROM events e
        WHERE e.event_id ~ '^[a-f0-9]{64}$'
          AND e.item_type = 'Folder'
          AND NOT EXISTS (
              SELECT 1 FROM events child
              WHERE child.parent_event_id = e.event_id
          )
        ORDER BY e.received_at
    """)

    logger.info("Phase 2: Found %d hex64 folder events with 0 children", len(rows))

    if not rows:
        return

    if dry_run:
        for r in rows:
            logger.info(
                "  [DRY RUN] Would requeue: %s — %s (total_children=%s, processed=%s)",
                r["event_id"], r["file_name"],
                r["folder_total_children"], r["folder_processed_children"],
            )
        return

    r = aioredis.from_url(redis_url)
    enqueued = 0
    errors = 0

    for row in rows:
        eid = row["event_id"]
        try:
            # Delete the event and all related rows so the worker can
            # recreate it fresh via create_event (which does INSERT).
            # Must cascade: child events first, then parent references.
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Delete child-related rows (none should exist, but be safe)
                    await conn.execute(
                        "DELETE FROM verdicts WHERE event_id IN "
                        "(SELECT event_id FROM events WHERE parent_event_id = $1)", eid)
                    await conn.execute(
                        "DELETE FROM audit_log WHERE event_id IN "
                        "(SELECT event_id FROM events WHERE parent_event_id = $1)", eid)
                    await conn.execute(
                        "DELETE FROM events WHERE parent_event_id = $1", eid)
                    # Delete parent-related rows
                    await conn.execute("DELETE FROM verdicts WHERE event_id = $1", eid)
                    await conn.execute("DELETE FROM audit_log WHERE event_id = $1", eid)
                    await conn.execute("DELETE FROM sharing_link_lifecycle WHERE event_id = $1", eid)
                    await conn.execute("DELETE FROM remediations WHERE event_id = $1", eid)
                    await conn.execute("DELETE FROM user_notifications WHERE event_id = $1", eid)
                    # Delete the event itself
                    await conn.execute("DELETE FROM events WHERE event_id = $1", eid)

            # Reconstruct job dict from DB columns
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

            await r.rpush(QUEUE_KEY, json.dumps(job_data))
            enqueued += 1
            logger.info("  Enqueued: %s — %s", eid, row["file_name"])

        except Exception:
            errors += 1
            logger.warning(
                "Failed to requeue event_id=%s", eid, exc_info=True,
            )

    await r.aclose()
    logger.info(
        "Phase 2 complete: %d enqueued, %d errors (of %d found)",
        enqueued, errors, len(rows),
    )


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

async def main() -> None:
    import os

    parser = argparse.ArgumentParser(
        description="Backfill folder verdicts and requeue hex64 folder events.",
    )
    parser.add_argument("--fix-verdicts", action="store_true",
                        help="Phase 1: fix parent folder verdicts with wrong escalation_tier")
    parser.add_argument("--requeue-folders", action="store_true",
                        help="Phase 2: requeue hex64 folder events with 0 children")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    args = parser.parse_args()

    if not args.fix_verdicts and not args.requeue_folders:
        parser.error("Specify at least one of --fix-verdicts or --requeue-folders")

    db_url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)

    try:
        if args.fix_verdicts:
            await fix_verdicts(pool, args.dry_run)

        if args.requeue_folders:
            await requeue_folders(pool, redis_url, args.dry_run)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
