"""Enqueue test events from test_data.csv into the Redis job queue."""

import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import redis

QUEUE_KEY = "sharesentinel:jobs"
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DEFAULT_COUNT = 100


def extract_filename(object_id: str) -> str:
    """Extract filename from the ObjectId URL."""
    path = urlparse(object_id).path
    return Path(path).name


def extract_user_id(object_id: str) -> str:
    """Try to extract a user identifier from the OneDrive URL."""
    # Pattern: /personal/<user_id>/Documents/...
    parts = urlparse(object_id).path.split("/")
    for i, part in enumerate(parts):
        if part == "personal" and i + 1 < len(parts):
            # Convert 10690343_contoso_com -> 10690343@contoso.com
            raw = parts[i + 1]
            segments = raw.split("_")
            if len(segments) >= 3:
                return f"{segments[0]}@{'.'.join(segments[1:])}"
            return raw
    return "unknown@unknown.com"


def extract_site_url(object_id: str) -> str:
    """Extract the site base URL from the ObjectId."""
    parsed = urlparse(object_id)
    path_parts = parsed.path.split("/")
    # Find /personal/<user>/ or /sites/<site>/
    for i, part in enumerate(path_parts):
        if part in ("personal", "sites") and i + 1 < len(path_parts):
            site_path = "/".join(path_parts[: i + 2])
            return f"{parsed.scheme}://{parsed.netloc}{site_path}/"
    return f"{parsed.scheme}://{parsed.netloc}/"


def infer_item_type(filename: str) -> str:
    """Guess File vs Folder from filename."""
    if "." in filename:
        return "File"
    return "Folder"


def build_job(row: dict, index: int) -> dict:
    """Build a QueueJob dict from a CSV row."""
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
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": {
            "result": {
                "Operation": operation,
                "ObjectId": object_id,
                "UserId": user_id,
                "ItemType": item_type,
                "SharingScope": scope,
            }
        },
    }


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COUNT
    offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    csv_path = "/app/test_data.csv"
    if not os.path.exists(csv_path):
        csv_path = "test_data.csv"

    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        enqueued = 0
        for i, row in enumerate(reader):
            if i < offset:
                continue
            if enqueued >= count:
                break
            job = build_job(row, i)
            r.rpush(QUEUE_KEY, json.dumps(job))
            enqueued += 1

    queue_len = r.llen(QUEUE_KEY)
    print(f"Enqueued {enqueued} jobs (offset={offset}). Queue length: {queue_len}")


if __name__ == "__main__":
    main()
