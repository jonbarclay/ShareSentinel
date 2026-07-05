"""Backfill ShareSentinel queue from an access-checkpoint CSV scan.

Reads an access-log export of objects that lived in formerly-overshared
locations (OneDrive personal sites, flagged SharePoint, public SharePoint
sites). The export is an *access log*, so each unique object appears on many
rows (one per access event / user / timestamp). This script deduplicates to
one job per unique object and enqueues analysis-only jobs for the worker.

Because the sharing links these objects once had have already been remediated,
the normal pipeline would skip them at the "no active sharing links" gate. The
jobs enqueued here therefore carry ``force_analysis=True`` (bypass that gate)
and ``skip_if_unprocessable=True`` (skip items we cannot content-analyse
instead of recording a low-signal filename-only verdict). Both flags are
honoured by the worker orchestrator.

The job ``operation`` is ``AccessCheckpointBackfill`` so the event-driven
drift/remediation detectors (which only fire on ``AnonymousLinkCreated`` /
``CompanyLinkCreated``) no-op, and no lifecycle enrollment is triggered.

Scope decisions (mirrors backfill_public_sites.py):
  * Ingest *everything* that the worker can actually content-analyse.
  * Skip site chrome (``__siteIcon__``, ``/_catalogs/`` theme files, ``.onetoc2``).
  * Skip anything the FileClassifier routes to FILENAME_ONLY (RAW photos, video
    parked as TRANSCRIPT, excluded binaries, no-extension, unknown types).

CSV format (header required) — the access-checkpoint schema:
    Source,ObjectId,MatchedTarget,Operation,AccessTimestampUTC,AccessedByUPN,
    UserType,IsInternal,ClientIP,Service,Tier,DetectedCategories,FileName,
    FlaggedWebUrl,ShareType,SharedWith,Site

Usage (inside the worker container):
    docker cp access_checkpoint_part1.csv sharesentinel-worker:/app/
    docker exec sharesentinel-worker python -m scripts.backfill_access_checkpoint --dry-run
    docker exec sharesentinel-worker python -m scripts.backfill_access_checkpoint \
        --source onedrive --limit 200 --batch-size 50 --batch-delay 3
    docker exec sharesentinel-worker python -m scripts.backfill_access_checkpoint \
        --source onedrive --batch-size 50 --batch-delay 3
"""

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urlunsplit, quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_access_checkpoint")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config
from app.pipeline.classifier import Action, FileClassifier

QUEUE_KEY = "sharesentinel:jobs"

# Some FileName / SharedWith values contain commas, so the access-log can have
# very long quoted fields. Raise the field-size limit so csv does not choke.
csv.field_size_limit(10 * 1024 * 1024)

# Classifier actions that mean "the worker can content-analyse this item".
# Everything else (FILENAME_ONLY, TRANSCRIPT_ANALYSIS) is skipped per the
# operator's "skip entirely, no filename-only fallback" choice.
ENQUEUE_ACTIONS = {
    Action.FULL_ANALYSIS,
    Action.MULTIMODAL,
    Action.ARCHIVE_MANIFEST,
    Action.FORMAT_CONVERSION,
    Action.PENDING_MANUAL,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill ShareSentinel queue from an access-checkpoint CSV scan",
    )
    parser.add_argument(
        "--csv",
        default="/app/access_checkpoint_part1.csv",
        help="Path to the CSV file (default: /app/access_checkpoint_part1.csv)",
    )
    parser.add_argument(
        "--source",
        default="onedrive",
        help="Comma-separated Source values to ingest (default: onedrive). "
             "Use 'all' to ingest every source.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be enqueued without enqueuing")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Jobs per batch (default: 50)")
    parser.add_argument("--batch-delay", type=float, default=3.0,
                        help="Seconds between batches (default: 3.0)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max total jobs to enqueue, 0=unlimited (default: 0)")
    parser.add_argument("--site-filter", default="",
                        help="Only enqueue URLs whose URL contains this string")
    return parser.parse_args()


def canonicalize_url(raw: str) -> str:
    """Percent-encode the path of a SharePoint/OneDrive object URL.

    The CSV contains URLs with literal spaces (and occasionally commas, etc.).
    Graph's ``/shares`` endpoint resolves most reliably against the canonical
    percent-encoded webUrl form, so normalise to that. Idempotent for URLs that
    are already encoded because ``quote`` leaves ``%`` untouched via ``safe``.
    """
    parts = urlsplit(raw.strip())
    encoded_path = quote(parts.path, safe="/%()!*'-._~&$+=@")
    return urlunsplit((parts.scheme, parts.netloc, encoded_path, parts.query, parts.fragment))


def derive_site_url(url: str) -> str:
    """Return the site-collection root URL for a SharePoint/OneDrive object URL."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    base = f"{parts.scheme}://{parts.netloc}"
    if len(segments) >= 2 and segments[0].lower() in ("sites", "teams", "personal"):
        return f"{base}/{segments[0]}/{segments[1]}"
    return base


def derive_file_name(url: str) -> str:
    """Return the (unquoted) final path segment as the file name."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    return unquote(segments[-1]) if segments else ""


def is_site_chrome(url: str, file_name: str) -> bool:
    """True for SharePoint site-chrome objects we never want to analyse."""
    lower_url = url.lower()
    lower_name = file_name.lower()
    if lower_name.startswith("__siteicon__"):
        return True
    if "/_catalogs/" in lower_url:
        return True
    if lower_name.endswith(".onetoc2"):
        return True
    return False


def build_event_id(canonical_url: str) -> str:
    """Deterministic event ID, namespaced to avoid collision with other sources."""
    return hashlib.sha256(
        f"access_checkpoint_backfill:{canonical_url}".encode("utf-8")
    ).hexdigest()


def build_job(canonical_url: str, agg: dict, csv_name: str) -> dict:
    """Construct a Redis job payload for one formerly-overshared object."""
    parts = urlsplit(canonical_url)
    return {
        "event_id": build_event_id(canonical_url),
        "operation": "AccessCheckpointBackfill",
        "workload": agg["workload"],
        # Empty actor: there is no sharing user for a backfill, and this skips
        # the per-event Graph user-profile enrichment lookups.
        "user_id": "",
        "object_id": canonical_url,
        "site_url": derive_site_url(canonical_url),
        "file_name": agg["file_name"],
        "relative_path": unquote(parts.path),
        "item_type": "File",
        "sharing_type": "FormerlyOvershared",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        # Pipeline behaviour flags (see module docstring / orchestrator).
        "force_analysis": True,
        "skip_if_unprocessable": True,
        "raw_payload": {
            "source": "access_checkpoint_backfill",
            "origin_source": agg["origin_source"],
            "csv_file": csv_name,
            "object_url": canonical_url,
            "access_event_count": agg["access_count"],
            "distinct_user_count": len(agg["users"]),
            "share_type_raw": agg["share_type"],
            "shared_with_raw": agg["shared_with"],
        },
    }


def workload_for_url(url: str) -> str:
    """OneDrive personal sites live on the *-my host; everything else is SharePoint."""
    host = urlsplit(url).netloc.lower()
    return "OneDrive" if "-my." in host else "SharePoint"


async def load_existing_ids(pool: asyncpg.Pool) -> tuple[set[str], set[str]]:
    """Load existing event_ids and object_ids from the events table."""
    async with pool.acquire() as conn:
        eid_rows = await conn.fetch("SELECT event_id FROM events")
        oid_rows = await conn.fetch(
            "SELECT DISTINCT object_id FROM events WHERE object_id IS NOT NULL"
        )
    return ({r["event_id"] for r in eid_rows},
            {r["object_id"] for r in oid_rows})


async def main() -> None:
    args = parse_args()

    # ── 1. Load CSV ──────────────────────────────────────────────────
    if not os.path.exists(args.csv):
        print(f"ERROR: CSV file not found: {args.csv}")
        sys.exit(1)

    wanted_sources = {s.strip().lower() for s in args.source.split(",") if s.strip()}
    ingest_all_sources = "all" in wanted_sources

    csv_name = os.path.basename(args.csv)
    classifier = FileClassifier()
    config = Config.from_env()

    # ── 2. Stream rows, aggregate per unique canonical ObjectId ──────
    # candidates: canonical_url -> aggregate dict
    candidates: dict[str, dict] = {}
    stats = Counter()
    skip_action_breakdown: Counter = Counter()

    logger.info("Loading CSV: %s (sources=%s)", args.csv, args.source)
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for required in ("Source", "ObjectId"):
            if required not in fieldnames:
                print(f"ERROR: CSV missing '{required}' column. Found: {fieldnames}")
                sys.exit(1)

        for row in reader:
            stats["total_rows"] += 1

            source = (row.get("Source") or "").strip().lower()
            if not ingest_all_sources and source not in wanted_sources:
                stats["filtered_source"] += 1
                continue

            raw_obj = (row.get("ObjectId") or "").strip()
            if not raw_obj:
                stats["blank"] += 1
                continue

            canonical = canonicalize_url(raw_obj)

            if args.site_filter and args.site_filter.lower() not in canonical.lower():
                stats["filtered_out"] += 1
                continue

            user = (row.get("AccessedByUPN") or "").strip().lower()

            # Already-seen unique object → just accumulate access signal.
            existing = candidates.get(canonical)
            if existing is not None:
                stats["duplicate_in_csv"] += 1
                if not existing.get("_drop"):
                    existing["access_count"] += 1
                    if user:
                        existing["users"].add(user)
                continue

            file_name = derive_file_name(canonical)

            # Site chrome — never analyse.
            if is_site_chrome(canonical, file_name):
                stats["skipped_chrome"] += 1
                # Mark as a non-candidate so repeat rows do not re-classify.
                candidates[canonical] = {"_drop": True}
                continue

            # Classify (size unknown at enqueue time → 0, so the size-based
            # oversized→FILENAME_ONLY route does not fire here; oversized items
            # are caught and skipped at runtime by skip_if_unprocessable).
            result = classifier.classify(file_name, "File", 0, config)
            if result.action not in ENQUEUE_ACTIONS:
                stats["skipped_unprocessable"] += 1
                skip_action_breakdown[result.category.value] += 1
                candidates[canonical] = {"_drop": True}
                continue

            candidates[canonical] = {
                "_drop": False,
                "file_name": file_name,
                "origin_source": source,
                "workload": workload_for_url(canonical),
                "access_count": 1,
                "users": {user} if user else set(),
                "share_type": (row.get("ShareType") or "").strip(),
                "shared_with": (row.get("SharedWith") or "").strip(),
            }

    # Drop the chrome/unprocessable sentinels.
    candidates = {u: a for u, a in candidates.items() if not a.get("_drop")}

    logger.info(
        "Candidates after source/chrome/type filtering: %d "
        "(skipped %d chrome, %d unprocessable)",
        len(candidates), stats["skipped_chrome"], stats["skipped_unprocessable"],
    )

    # ── 3. Connect to DB and drop already-known objects ──────────────
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    logger.info("Connected to database, loading existing events...")
    existing_event_ids, existing_object_ids = await load_existing_ids(pool)
    logger.info(
        "Database has %d event IDs and %d distinct object IDs",
        len(existing_event_ids), len(existing_object_ids),
    )
    await pool.close()

    jobs_to_enqueue: list[dict] = []
    for canonical, agg in candidates.items():
        eid = build_event_id(canonical)
        if eid in existing_event_ids:
            stats["skipped_event_in_db"] += 1
            continue
        if canonical in existing_object_ids:
            stats["skipped_object_in_db"] += 1
            continue
        jobs_to_enqueue.append(build_job(canonical, agg, csv_name))

    # ── 4. Apply limit ───────────────────────────────────────────────
    if args.limit > 0 and len(jobs_to_enqueue) > args.limit:
        jobs_to_enqueue = jobs_to_enqueue[: args.limit]
        logger.info("Limited to %d jobs (--limit)", len(jobs_to_enqueue))

    # ── 5. Summary ───────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("  ShareSentinel Backfill: Access Checkpoint")
    print("=" * 62)
    print(f"  Source(s) ingested:            {args.source:>8}")
    print(f"  CSV rows scanned:              {stats['total_rows']:>8,}")
    print(f"  Filtered out (source):         {stats['filtered_source']:>8,}")
    print(f"  Blank ObjectId:                {stats['blank']:>8,}")
    print(f"  Duplicate access rows merged:  {stats['duplicate_in_csv']:>8,}")
    if args.site_filter:
        print(f"  Filtered out (site-filter):    {stats['filtered_out']:>8,}")
    print(f"  Skipped — site chrome:         {stats['skipped_chrome']:>8,}")
    print(f"  Skipped — unprocessable type:  {stats['skipped_unprocessable']:>8,}")
    if skip_action_breakdown:
        for cat, n in skip_action_breakdown.most_common():
            print(f"        · {cat:<22} {n:>8,}")
    print(f"  Skipped — event_id in DB:      {stats['skipped_event_in_db']:>8,}")
    print(f"  Skipped — object_id in DB:     {stats['skipped_object_in_db']:>8,}")
    print(f"  ───────────────────────────────────────────")
    print(f"  Jobs to enqueue:               {len(jobs_to_enqueue):>8,}")
    print("=" * 62)

    if not jobs_to_enqueue:
        print("\nNothing to enqueue. All items already processed or filtered out.")
        return

    if args.dry_run:
        print(f"\n[dry-run] Would enqueue {len(jobs_to_enqueue)} jobs. First 30:")
        print(f"{'#':<4} {'Src':<10} {'Acc':>5}  {'File'}")
        print("-" * 100)
        for i, j in enumerate(jobs_to_enqueue[:30], 1):
            rp = j["raw_payload"]
            print(f"{i:<4} {rp['origin_source']:<10} {rp['access_event_count']:>5}  "
                  f"{j['file_name'][:70]}")
        if len(jobs_to_enqueue) > 30:
            print(f"... and {len(jobs_to_enqueue) - 30} more")
        print("\n[dry-run] No changes made.")
        return

    # ── 6. Enqueue to Redis ──────────────────────────────────────────
    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)
    enqueued = 0
    total_batches = (len(jobs_to_enqueue) + args.batch_size - 1) // args.batch_size

    for batch_num in range(total_batches):
        start = batch_num * args.batch_size
        batch = jobs_to_enqueue[start:start + args.batch_size]

        pipe = redis_conn.pipeline()
        for job in batch:
            pipe.rpush(QUEUE_KEY, json.dumps(job))
        await pipe.execute()

        enqueued += len(batch)
        queue_depth = await redis_conn.llen(QUEUE_KEY)
        logger.info(
            "[Batch %d/%d] Enqueued %d (total: %d, queue depth: %d)",
            batch_num + 1, total_batches, len(batch), enqueued, queue_depth,
        )
        if batch_num < total_batches - 1 and args.batch_delay > 0:
            await asyncio.sleep(args.batch_delay)

    queue_depth = await redis_conn.llen(QUEUE_KEY)
    print(f"\nEnqueued {enqueued} jobs to {QUEUE_KEY} (queue depth: {queue_depth})")
    print("\nMonitor progress with: docker compose logs -f worker")
    await redis_conn.aclose()


if __name__ == "__main__":
    asyncio.run(main())
