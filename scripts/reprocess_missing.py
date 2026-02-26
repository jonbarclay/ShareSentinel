"""Re-enqueue events from test_data.csv that are missing from the database.

Reads the CSV, builds job payloads the same way enqueue_test_data.py does,
then checks each event_id against the DB.  Only events that do NOT exist
in the events table are enqueued.

Usage (inside worker container):
    python -m scripts.reprocess_missing [--dry-run]
"""

import asyncio
import csv
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reprocess_missing")

import asyncpg
import redis.asyncio as aioredis

from app.config import Config

QUEUE_KEY = "sharesentinel:jobs"


def extract_filename(object_id: str) -> str:
    path = urlparse(object_id).path
    return Path(path).name


def extract_user_id(object_id: str) -> str:
    parts = urlparse(object_id).path.split("/")
    for i, part in enumerate(parts):
        if part == "personal" and i + 1 < len(parts):
            raw = parts[i + 1]
            segments = raw.split("_")
            if len(segments) >= 3:
                return f"{segments[0]}@{'.'.join(segments[1:])}"
            return raw
    return "unknown@unknown.com"


def extract_site_url(object_id: str) -> str:
    parsed = urlparse(object_id)
    path_parts = parsed.path.split("/")
    for i, part in enumerate(path_parts):
        if part in ("personal", "sites") and i + 1 < len(path_parts):
            site_path = "/".join(path_parts[: i + 2])
            return f"{parsed.scheme}://{parsed.netloc}{site_path}/"
    return f"{parsed.scheme}://{parsed.netloc}/"


def infer_item_type(filename: str) -> str:
    if "." in filename:
        return "File"
    return "Folder"


def build_job(row: dict) -> dict:
    object_id = row["properties.RawEventData.ObjectId"]
    scope = row["properties.RawEventData.SharingLinkScope"]
    unique_id = row["properties.RawEventData.UniqueSharingId"]

    filename = extract_filename(object_id)
    item_type = infer_item_type(filename)
    user_id = extract_user_id(object_id)
    site_url = extract_site_url(object_id)

    operation = (
        "AnonymousLinkCreated" if scope == "Anonymous"
        else "CompanySharingLinkCreated"
    )
    sharing_type = "Anonymous" if scope == "Anonymous" else "Company"

    return {
        "event_id": unique_id or str(uuid.uuid4()),
        "operation": operation,
        "workload": "OneDrive" if "-my.sharepoint.com" in object_id else "SharePoint",
        "user_id": user_id,
        "object_id": object_id,
        "site_url": site_url,
        "file_name": filename,
        "relative_path": urlparse(object_id).path,
        "item_type": item_type,
        "sharing_type": sharing_type,
        "sharing_scope": scope,
        "sharing_permission": "View",
        "event_time": datetime.now(timezone.utc).isoformat(),
    }


async def main() -> None:
    dry_run = "--dry-run" in sys.argv

    config = Config.from_env()
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)

    # Get all event_ids currently in the database
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT event_id FROM events")
    existing_ids = {r["event_id"] for r in rows}
    logger.info("Found %d existing events in database", len(existing_ids))

    # Read CSV and find missing events
    csv_path = "/app/test_data.csv"
    if not os.path.exists(csv_path):
        csv_path = "test_data.csv"

    missing_jobs = []
    total_csv = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_csv += 1
            job = build_job(row)
            if job["event_id"] not in existing_ids:
                missing_jobs.append(job)

    logger.info(
        "CSV has %d rows, %d already in DB, %d missing (to reprocess)",
        total_csv, total_csv - len(missing_jobs), len(missing_jobs),
    )

    if not missing_jobs:
        print("All CSV events already exist in the database. Nothing to reprocess.")
        await pool.close()
        return

    if dry_run:
        # Show breakdown
        folders = sum(1 for j in missing_jobs if j["item_type"] == "Folder")
        files = len(missing_jobs) - folders
        print(f"\n[dry-run] {len(missing_jobs)} missing events ({files} files, {folders} folders)")
        print(f"\nFirst 20:")
        print(f"{'#':<4} {'Event ID':<40} {'Type':<8} {'File':<50}")
        print("-" * 102)
        for i, j in enumerate(missing_jobs[:20], 1):
            print(f"{i:<4} {j['event_id'][:39]:<40} {j['item_type']:<8} {(j['file_name'] or '')[:49]:<50}")
        if len(missing_jobs) > 20:
            print(f"... and {len(missing_jobs) - 20} more")
        print(f"\n[dry-run] No changes made.")
        await pool.close()
        return

    # Enqueue
    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)
    enqueued = 0
    for job in missing_jobs:
        await redis_conn.rpush(QUEUE_KEY, json.dumps(job))
        enqueued += 1

    logger.info("Enqueued %d jobs to %s", enqueued, QUEUE_KEY)
    print(f"\nEnqueued {enqueued} events for reprocessing.")
    print("Monitor progress with: docker compose logs -f worker")

    await redis_conn.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
