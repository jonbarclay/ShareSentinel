"""Backfill ShareSentinel queue from a formerly-public SharePoint sites scan.

Reads a CSV export of objects that lived in SharePoint sites that were at one
point accessible to *any* UVU user (public site visibility), even though they
were NOT explicitly shared via anonymous or org-wide sharing links.

Because these items have no sharing links, the normal pipeline would skip them
at the "no active sharing links" gate. The jobs enqueued here therefore carry
``force_analysis=True`` (bypass that gate) and ``skip_if_unprocessable=True``
(skip items we cannot content-analyse instead of recording a low-signal
filename-only verdict). Both flags are honoured by the worker orchestrator.

Scope decisions (per operator request):
  * Ingest *everything* that the worker can actually content-analyse.
  * Skip site chrome (``__siteIcon__``, ``/_catalogs/`` theme files, ``.onetoc2``).
  * Skip anything the FileClassifier routes to FILENAME_ONLY (RAW photos, video
    parked as TRANSCRIPT, excluded binaries, no-extension, unknown types).

Classification is delegated to the worker's own FileClassifier so this script
stays in lock-step with the pipeline's notion of "processable".

CSV format (header required):
    ObjectId,UserCount
    https://uvu365.sharepoint.com/sites/Foo/Shared Documents/General/x.docx,5

Usage (inside the worker container):
    docker cp public-sites_unique_objects.csv sharesentinel-worker:/app/
    docker exec sharesentinel-worker python -m scripts.backfill_public_sites --dry-run
    docker exec sharesentinel-worker python -m scripts.backfill_public_sites \
        --batch-size 50 --batch-delay 3
    docker exec sharesentinel-worker python -m scripts.backfill_public_sites \
        --site-filter FacultyConvocation --dry-run
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
logger = logging.getLogger("backfill_public_sites")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config
from app.pipeline.classifier import Action, FileClassifier

QUEUE_KEY = "sharesentinel:jobs"

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
        description="Backfill ShareSentinel queue from a formerly-public-sites CSV scan",
    )
    parser.add_argument(
        "--csv",
        default="/app/public-sites_unique_objects.csv",
        help="Path to the CSV file (default: /app/public-sites_unique_objects.csv)",
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
                        help="Only enqueue URLs whose site URL contains this string")
    return parser.parse_args()


def canonicalize_url(raw: str) -> str:
    """Percent-encode the path of a SharePoint object URL.

    The CSV contains URLs with literal spaces (and occasionally commas, etc.).
    Graph's ``/shares`` endpoint resolves most reliably against the canonical
    percent-encoded webUrl form, so normalise to that. Idempotent for URLs that
    are already encoded because ``quote`` leaves ``%`` untouched via ``safe``.
    """
    parts = urlsplit(raw.strip())
    # Preserve path separators and characters SharePoint leaves unencoded.
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
        f"public_site_backfill:{canonical_url}".encode("utf-8")
    ).hexdigest()


def build_job(canonical_url: str, file_name: str, user_count: int, csv_name: str) -> dict:
    """Construct a Redis job payload for one formerly-public-site object."""
    parts = urlsplit(canonical_url)
    return {
        "event_id": build_event_id(canonical_url),
        "operation": "PublicSiteBackfill",
        "workload": "SharePoint",
        # Empty actor: there is no sharing user, and this skips the per-event
        # Graph user-profile enrichment lookups.
        "user_id": "",
        "object_id": canonical_url,
        "site_url": derive_site_url(canonical_url),
        "file_name": file_name,
        "relative_path": unquote(parts.path),
        "item_type": "File",
        "sharing_type": "FormerlyPublicSite",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        # Pipeline behaviour flags (see module docstring / orchestrator).
        "force_analysis": True,
        "skip_if_unprocessable": True,
        "raw_payload": {
            "source": "public_site_backfill",
            "csv_file": csv_name,
            "object_url": canonical_url,
            "former_user_count": user_count,
        },
    }


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

    csv_name = os.path.basename(args.csv)
    logger.info("Loading CSV: %s", args.csv)
    rows: list[dict] = []
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "ObjectId" not in (reader.fieldnames or []):
            print(f"ERROR: CSV missing 'ObjectId' column. Found: {reader.fieldnames}")
            sys.exit(1)
        for row in reader:
            rows.append(row)
    logger.info("Loaded %d rows from CSV", len(rows))

    # ── 2. Classify each object & decide enqueue vs skip ─────────────
    classifier = FileClassifier()
    config = Config.from_env()

    candidates: dict[str, dict] = {}   # canonical_url -> {file_name, user_count}
    stats = Counter()
    skip_action_breakdown: Counter = Counter()
    seen_urls: set[str] = set()

    for row in rows:
        stats["total_rows"] += 1
        raw_obj = (row.get("ObjectId") or "").strip()
        if not raw_obj:
            stats["blank"] += 1
            continue

        try:
            user_count = int((row.get("UserCount") or "0").strip() or 0)
        except ValueError:
            user_count = 0

        canonical = canonicalize_url(raw_obj)
        file_name = derive_file_name(canonical)

        if args.site_filter and args.site_filter.lower() not in canonical.lower():
            stats["filtered_out"] += 1
            continue

        if canonical in seen_urls:
            stats["duplicate_in_csv"] += 1
            continue
        seen_urls.add(canonical)

        # Site chrome — never analyse.
        if is_site_chrome(canonical, file_name):
            stats["skipped_chrome"] += 1
            continue

        # Classify (size unknown at enqueue time → 0, so the size-based
        # oversized→FILENAME_ONLY route does not fire here; oversized items
        # are caught and skipped at runtime by skip_if_unprocessable).
        result = classifier.classify(file_name, "File", 0, config)
        if result.action not in ENQUEUE_ACTIONS:
            stats["skipped_unprocessable"] += 1
            skip_action_breakdown[result.category.value] += 1
            continue

        candidates[canonical] = {"file_name": file_name, "user_count": user_count}

    logger.info(
        "Candidates after chrome/type filtering: %d (skipped %d chrome, %d unprocessable)",
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
    for canonical, meta in candidates.items():
        eid = build_event_id(canonical)
        if eid in existing_event_ids:
            stats["skipped_event_in_db"] += 1
            continue
        if canonical in existing_object_ids:
            stats["skipped_object_in_db"] += 1
            continue
        jobs_to_enqueue.append(
            build_job(canonical, meta["file_name"], meta["user_count"], csv_name)
        )

    # ── 4. Apply limit ───────────────────────────────────────────────
    if args.limit > 0 and len(jobs_to_enqueue) > args.limit:
        jobs_to_enqueue = jobs_to_enqueue[: args.limit]
        logger.info("Limited to %d jobs (--limit)", len(jobs_to_enqueue))

    # ── 5. Summary ───────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("  ShareSentinel Backfill: Formerly-Public Sites")
    print("=" * 62)
    print(f"  CSV rows loaded:               {stats['total_rows']:>8,}")
    print(f"  Blank ObjectId:                {stats['blank']:>8,}")
    print(f"  Duplicate URL in CSV:          {stats['duplicate_in_csv']:>8,}")
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
        print(f"{'#':<4} {'Site':<38} {'File'}")
        print("-" * 110)
        for i, j in enumerate(jobs_to_enqueue[:30], 1):
            site = j["site_url"].replace("https://uvu365.sharepoint.com/sites/", "")
            print(f"{i:<4} {site[:36]:<38} {j['file_name'][:64]}")
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
