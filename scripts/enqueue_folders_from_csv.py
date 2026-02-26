"""Enqueue only folder events from test_data.csv into the Redis job queue.

Usage (inside worker container):
    python -m scripts.enqueue_folders_from_csv [--limit N] [--dry-run]
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import redis

QUEUE_KEY = "sharesentinel:jobs"
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def extract_filename(object_id: str) -> str:
    return Path(urlparse(object_id).path).name


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


def is_folder(filename: str) -> bool:
    return "." not in filename


def build_job(row: dict) -> dict:
    object_id = row["properties.RawEventData.ObjectId"]
    scope = row["properties.RawEventData.SharingLinkScope"]
    unique_id = row["properties.RawEventData.UniqueSharingId"]
    filename = extract_filename(object_id)
    user_id = extract_user_id(object_id)
    site_url = extract_site_url(object_id)
    operation = "AnonymousLinkCreated" if scope == "Anonymous" else "CompanySharingLinkCreated"
    sharing_type = "Anonymous" if scope == "Anonymous" else "Company"

    return {
        "event_id": unique_id,
        "operation": operation,
        "workload": "OneDrive" if "-my.sharepoint.com" in object_id else "SharePoint",
        "user_id": user_id,
        "object_id": object_id,
        "site_url": site_url,
        "file_name": filename,
        "relative_path": urlparse(object_id).path,
        "item_type": "Folder",
        "sharing_type": sharing_type,
        "sharing_scope": scope,
        "sharing_permission": "View",
        "event_time": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    csv_path = "/app/test_data.csv"
    if not os.path.exists(csv_path):
        csv_path = "test_data.csv"

    folders = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            filename = extract_filename(row["properties.RawEventData.ObjectId"])
            if is_folder(filename):
                folders.append(row)

    if args.limit:
        folders = folders[: args.limit]

    print(f"Found {len(folders)} folder events")

    if args.dry_run:
        for row in folders[:10]:
            fn = extract_filename(row["properties.RawEventData.ObjectId"])
            print(f"  [DRY RUN] {row['properties.RawEventData.UniqueSharingId']} — {fn}")
        if len(folders) > 10:
            print(f"  ... and {len(folders) - 10} more")
        return

    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()

    for row in folders:
        job = build_job(row)
        r.rpush(QUEUE_KEY, json.dumps(job))

    queue_len = r.llen(QUEUE_KEY)
    print(f"Enqueued {len(folders)} folder jobs. Queue length: {queue_len}")


if __name__ == "__main__":
    main()
