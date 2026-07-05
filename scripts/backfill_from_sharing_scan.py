"""Backfill ShareSentinel queue from a broad-sharing CSV scan.

Reads a CSV export of SharePoint files/folders with anonymous or org-wide
sharing links, deduplicates by unique sharing URL, checks against the
database for already-processed items, and enqueues new jobs for the worker.

Folder sharing URLs (containing ':f:') are enqueued as item_type=Folder so
the worker enumerates and processes every child file automatically.  File
URLs from rows that have NO folder-level sharing link are enqueued as
item_type=File.

Usage (inside worker container):
    python -m scripts.backfill_from_sharing_scan --dry-run
    python -m scripts.backfill_from_sharing_scan --folders-only --batch-size 20 --batch-delay 5
    python -m scripts.backfill_from_sharing_scan --files-only --batch-size 50 --batch-delay 2
    python -m scripts.backfill_from_sharing_scan --site-filter MachineShoppictures --dry-run
"""

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_sharing_scan")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config

QUEUE_KEY = "sharesentinel:jobs"

# URL type code indicating a folder sharing link
FOLDER_TYPE_CODE = ":f:"

# Mapping from ShareType suffix to sharing_permission
PERMISSION_MAP = {
    "edit": "Edit",
    "view": "View",
    "createonly": "CreateOnly",
    "blocksdownload": "BlocksDownload",
    "review": "Review",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill ShareSentinel queue from broad-sharing CSV scan"
    )
    parser.add_argument(
        "--csv",
        default="/app/BroadSharing_Filtered_20260409.csv",
        help="Path to the CSV file (default: /app/BroadSharing_Filtered_20260409.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be enqueued without enqueuing",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Jobs per batch (default: 50)",
    )
    parser.add_argument(
        "--batch-delay",
        type=float,
        default=2.0,
        help="Seconds between batches (default: 2.0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max total jobs to enqueue, 0=unlimited (default: 0)",
    )
    parser.add_argument(
        "--folders-only",
        action="store_true",
        help="Only enqueue folder URLs",
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="Only enqueue file URLs (not covered by folder links)",
    )
    parser.add_argument(
        "--site-filter",
        default="",
        help="Only enqueue URLs whose site URL contains this string",
    )
    return parser.parse_args()


def build_event_id(sharing_url: str) -> str:
    """Deterministic event ID from a sharing URL.

    Namespaced with 'backfill:sharing_scan:' to avoid collision with
    audit-poller event IDs which hash different inputs.
    """
    return hashlib.sha256(
        f"backfill:sharing_scan:{sharing_url}".encode("utf-8")
    ).hexdigest()


def parse_share_type(share_type_raw: str) -> tuple[str, str, str, str]:
    """Map the CSV ShareType field to (operation, sharing_type, sharing_scope, sharing_permission).

    When multiple types are present (semicolon-separated), prefer Anonymous
    over Org (higher risk), and Edit over View (higher permission).
    """
    parts = [p.strip() for p in share_type_raw.split(";") if p.strip()]

    has_anonymous = False
    best_permission = "View"
    permission_priority = {"edit": 4, "createonly": 3, "review": 2, "view": 1, "blocksdownload": 0}

    for part in parts:
        lower = part.lower()
        if lower.startswith("anonymouslink-"):
            has_anonymous = True
        suffix = lower.split("-", 1)[1] if "-" in lower else "view"
        prio = permission_priority.get(suffix, 0)
        if prio > permission_priority.get(best_permission.lower(), 0):
            best_permission = PERMISSION_MAP.get(suffix, "View")

    if has_anonymous:
        return ("AnonymousLinkCreated", "Anonymous", "Anyone", best_permission)
    else:
        return ("CompanySharingLinkCreated", "Company", "Organization", best_permission)


def is_folder_url(url: str) -> bool:
    """Check if a sharing URL is a folder-level link."""
    return FOLDER_TYPE_CODE in url


def parse_csv_datetime(dt_str: str) -> str:
    """Parse CSV datetime like '8/12/2019 4:26:29 PM' to ISO8601."""
    if not dt_str or not dt_str.strip():
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = datetime.strptime(dt_str.strip(), "%m/%d/%Y %I:%M:%S %p")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def extract_unique_urls(
    rows: list[dict],
) -> tuple[dict[str, dict], dict[str, dict], dict]:
    """Extract and deduplicate sharing URLs from CSV rows.

    Returns:
        (folder_urls, file_only_urls, stats)
        - folder_urls: {url: first_row_metadata} for folder sharing URLs
        - file_only_urls: {url: first_row_metadata} for file URLs from rows
          that have NO folder URL in their BroadSharingLinkUrls
        - stats: dict with counts for logging
    """
    folder_urls: dict[str, dict] = {}
    file_only_urls: dict[str, dict] = {}
    stats = {
        "total_rows": 0,
        "rows_with_folder_url": 0,
        "rows_file_only": 0,
        "total_url_occurrences": 0,
        "duplicate_url_skips": 0,
    }

    for row in rows:
        stats["total_rows"] += 1
        raw_urls = row.get("BroadSharingLinkUrls", "").strip()
        if not raw_urls:
            continue

        urls = [u.strip() for u in raw_urls.split(";") if u.strip()]
        row_has_folder = any(is_folder_url(u) for u in urls)

        if row_has_folder:
            stats["rows_with_folder_url"] += 1
        else:
            stats["rows_file_only"] += 1

        for url in urls:
            stats["total_url_occurrences"] += 1

            if is_folder_url(url):
                if url not in folder_urls:
                    folder_urls[url] = row
                else:
                    stats["duplicate_url_skips"] += 1
            elif not row_has_folder:
                # Only track file URLs from rows with NO folder URL
                if url not in file_only_urls:
                    file_only_urls[url] = row
                else:
                    stats["duplicate_url_skips"] += 1
            else:
                # File URL on a row that also has a folder URL — skip,
                # the folder enumeration will cover this file.
                stats["duplicate_url_skips"] += 1

    return folder_urls, file_only_urls, stats


def build_job(sharing_url: str, row: dict, item_type: str) -> dict:
    """Construct a Redis job payload from a sharing URL and CSV row metadata."""
    event_id = build_event_id(sharing_url)
    operation, sharing_type, sharing_scope, sharing_permission = parse_share_type(
        row.get("ShareType", "OrgLink-view")
    )

    path_str = row.get("Path", "")
    relative_path = urlparse(path_str).path if path_str else ""

    return {
        "event_id": event_id,
        "operation": operation,
        "workload": "SharePoint",
        "user_id": "backfill@sharesentinel.local",
        "object_id": sharing_url,
        "site_url": row.get("SPSiteUrl", ""),
        "file_name": row.get("Filename", ""),
        "relative_path": relative_path,
        "item_type": item_type,
        "sharing_type": sharing_type,
        "sharing_scope": sharing_scope,
        "sharing_permission": sharing_permission,
        "event_time": parse_csv_datetime(row.get("Created", "")),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": {
            "source": "backfill_from_sharing_scan",
            "csv_file": os.path.basename(
                row.get("_csv_path", "BroadSharing_Filtered_20260409.csv")
            ),
            "original_path": path_str,
            "share_type_raw": row.get("ShareType", ""),
            "viewable_by_external": row.get("ViewableByExternalUsers", ""),
            "checked_at": row.get("CheckedDateTime", ""),
        },
    }


async def load_existing_ids(pool: asyncpg.Pool) -> tuple[set[str], set[str]]:
    """Load existing event_ids and object_ids from the events table."""
    async with pool.acquire() as conn:
        eid_rows = await conn.fetch("SELECT event_id FROM events")
        oid_rows = await conn.fetch(
            "SELECT DISTINCT object_id FROM events WHERE object_id IS NOT NULL"
        )

    existing_event_ids = {r["event_id"] for r in eid_rows}
    existing_object_ids = {r["object_id"] for r in oid_rows}
    return existing_event_ids, existing_object_ids


async def main() -> None:
    args = parse_args()

    if args.folders_only and args.files_only:
        print("ERROR: --folders-only and --files-only are mutually exclusive.")
        sys.exit(1)

    # ── 1. Load CSV ──────────────────────────────────────────────────
    csv_path = args.csv
    if not os.path.exists(csv_path):
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    logger.info("Loading CSV: %s", csv_path)
    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_csv_path"] = csv_path
            rows.append(row)
    logger.info("Loaded %d rows from CSV", len(rows))

    # ── 2. Extract & deduplicate URLs ────────────────────────────────
    folder_urls, file_only_urls, stats = extract_unique_urls(rows)
    logger.info(
        "Unique URLs: %d folder, %d file-only (from %d total URL occurrences)",
        len(folder_urls),
        len(file_only_urls),
        stats["total_url_occurrences"],
    )
    logger.info(
        "Rows: %d with folder URL, %d file-only",
        stats["rows_with_folder_url"],
        stats["rows_file_only"],
    )

    # ── 3. Apply filters ─────────────────────────────────────────────
    if args.folders_only:
        file_only_urls = {}
    elif args.files_only:
        folder_urls = {}

    if args.site_filter:
        pattern = args.site_filter.lower()
        folder_urls = {
            u: r for u, r in folder_urls.items()
            if pattern in r.get("SPSiteUrl", "").lower()
        }
        file_only_urls = {
            u: r for u, r in file_only_urls.items()
            if pattern in r.get("SPSiteUrl", "").lower()
        }
        logger.info(
            "After site filter '%s': %d folder, %d file URLs",
            args.site_filter,
            len(folder_urls),
            len(file_only_urls),
        )

    # ── 4. Connect to DB and load existing events ────────────────────
    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    logger.info("Connected to database, loading existing events...")
    existing_event_ids, existing_object_ids = await load_existing_ids(pool)
    logger.info(
        "Database has %d event IDs and %d distinct object IDs",
        len(existing_event_ids),
        len(existing_object_ids),
    )

    # ── 5. Filter out already-processed URLs ─────────────────────────
    jobs_to_enqueue: list[dict] = []
    skipped_event_id = 0
    skipped_object_id = 0

    for url, row in folder_urls.items():
        eid = build_event_id(url)
        if eid in existing_event_ids:
            skipped_event_id += 1
            continue
        if url in existing_object_ids:
            skipped_object_id += 1
            continue
        jobs_to_enqueue.append(build_job(url, row, "Folder"))

    for url, row in file_only_urls.items():
        eid = build_event_id(url)
        if eid in existing_event_ids:
            skipped_event_id += 1
            continue
        if url in existing_object_ids:
            skipped_object_id += 1
            continue
        jobs_to_enqueue.append(build_job(url, row, "File"))

    await pool.close()

    folder_jobs = sum(1 for j in jobs_to_enqueue if j["item_type"] == "Folder")
    file_jobs = len(jobs_to_enqueue) - folder_jobs

    # ── 6. Apply limit ───────────────────────────────────────────────
    if args.limit > 0 and len(jobs_to_enqueue) > args.limit:
        jobs_to_enqueue = jobs_to_enqueue[: args.limit]
        folder_jobs = sum(1 for j in jobs_to_enqueue if j["item_type"] == "Folder")
        file_jobs = len(jobs_to_enqueue) - folder_jobs
        logger.info("Limited to %d jobs (--limit %d)", len(jobs_to_enqueue), args.limit)

    # ── 7. Summary ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  ShareSentinel Backfill: Sharing Scan CSV")
    print("=" * 60)
    print(f"  CSV rows loaded:              {stats['total_rows']:>8,}")
    print(f"  Total URL occurrences:        {stats['total_url_occurrences']:>8,}")
    print(f"  Unique folder URLs:           {len(folder_urls):>8,}")
    print(f"  Unique file-only URLs:        {len(file_only_urls):>8,}")
    print(f"  Skipped (event_id in DB):     {skipped_event_id:>8,}")
    print(f"  Skipped (object_id in DB):    {skipped_object_id:>8,}")
    print(f"  ─────────────────────────────────────────")
    print(f"  Jobs to enqueue:              {len(jobs_to_enqueue):>8,}")
    print(f"    Folder jobs:                {folder_jobs:>8,}")
    print(f"    File jobs:                  {file_jobs:>8,}")
    print("=" * 60)

    if not jobs_to_enqueue:
        print("\nNothing to enqueue. All items already processed or filtered out.")
        return

    if args.dry_run:
        print(f"\n[dry-run] Would enqueue {len(jobs_to_enqueue)} jobs.")
        print(f"\nFirst 30:")
        print(f"{'#':<4} {'Type':<8} {'Operation':<28} {'Site':<40} {'File/URL'}")
        print("-" * 130)
        for i, j in enumerate(jobs_to_enqueue[:30], 1):
            site = j["site_url"].replace("https://uvu365.sharepoint.com/sites/", "")
            name = j["file_name"] or j["object_id"][-50:]
            print(f"{i:<4} {j['item_type']:<8} {j['operation']:<28} {site:<40} {name}")
        if len(jobs_to_enqueue) > 30:
            print(f"... and {len(jobs_to_enqueue) - 30} more")
        print(f"\n[dry-run] No changes made.")
        return

    # ── 8. Enqueue to Redis ──────────────────────────────────────────
    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)

    enqueued = 0
    total_batches = (len(jobs_to_enqueue) + args.batch_size - 1) // args.batch_size

    for batch_num in range(total_batches):
        start = batch_num * args.batch_size
        end = min(start + args.batch_size, len(jobs_to_enqueue))
        batch = jobs_to_enqueue[start:end]

        pipe = redis_conn.pipeline()
        for job in batch:
            pipe.rpush(QUEUE_KEY, json.dumps(job))
        await pipe.execute()

        enqueued += len(batch)
        queue_depth = await redis_conn.llen(QUEUE_KEY)
        logger.info(
            "[Batch %d/%d] Enqueued %d jobs (total: %d, queue depth: %d)",
            batch_num + 1,
            total_batches,
            len(batch),
            enqueued,
            queue_depth,
        )

        if batch_num < total_batches - 1 and args.batch_delay > 0:
            await asyncio.sleep(args.batch_delay)

    print(f"\nEnqueued {enqueued} jobs to {QUEUE_KEY}")
    print(f"  Folder jobs: {folder_jobs}")
    print(f"  File jobs:   {file_jobs}")

    queue_depth = await redis_conn.llen(QUEUE_KEY)
    print(f"  Queue depth: {queue_depth}")
    print("\nMonitor progress with: docker compose logs -f worker")

    await redis_conn.aclose()


if __name__ == "__main__":
    asyncio.run(main())
