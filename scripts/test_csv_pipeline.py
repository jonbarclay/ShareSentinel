#!/usr/bin/env python3
"""Process test_data.csv rows through the ShareSentinel pipeline.

Modes:
  --dry-run           Parse all rows, print summary (file types, counts, excluded)
  --single N          Process row N through the full pipeline
  --batch [--limit N] Process rows sequentially with optional limit
  --enqueue           Push jobs onto Redis queue for the worker container
  --skip-excluded     Filter out video/audio/binary file types before processing
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

# Add project root and worker service to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "worker"))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

# Excluded extensions from file_types.yml (video/audio/binary/etc.)
EXCLUDED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma",
    ".exe", ".dll", ".bin", ".msi", ".app", ".dmg",
    ".mdb", ".accdb", ".sqlite", ".db",
    ".psd", ".ai", ".sketch", ".fig",
    ".dwg", ".dxf", ".stl", ".obj",
    ".ttf", ".otf", ".woff",
}

CSV_FILE = PROJECT_ROOT / "test_data.csv"


def parse_object_id(url: str) -> dict:
    """Parse an ObjectId URL into its components.

    OneDrive personal: /personal/{employee_id}_example_edu/Documents/...
    SharePoint sites:  /sites/{site_name}/Shared Documents/...
    """
    parsed = urlparse(unquote(url))
    path = parsed.path

    workload = "OneDrive"
    user_id = ""
    site_name = ""
    file_name = ""
    relative_path = ""
    site_url = f"{parsed.scheme}://{parsed.netloc}"

    # OneDrive personal
    m = re.match(r"/personal/([^/]+)/Documents/(.*)", path)
    if m:
        raw_user = m.group(1)
        # Convert 10690343_example_edu -> 10690343
        user_id = raw_user.split("_")[0] if "_" in raw_user else raw_user
        remainder = m.group(2)
        file_name = PurePosixPath(remainder).name
        relative_path = remainder
        site_url += f"/personal/{raw_user}"
        return {
            "workload": workload,
            "user_id": user_id,
            "file_name": file_name,
            "relative_path": relative_path,
            "site_url": site_url,
            "site_name": "",
        }

    # SharePoint site
    m = re.match(r"/sites/([^/]+)/Shared Documents/(.*)", path)
    if m:
        workload = "SharePoint"
        site_name = m.group(1)
        remainder = m.group(2)
        file_name = PurePosixPath(remainder).name
        relative_path = remainder
        site_url += f"/sites/{site_name}"
        return {
            "workload": workload,
            "user_id": "",
            "file_name": file_name,
            "relative_path": relative_path,
            "site_url": site_url,
            "site_name": site_name,
        }

    # Fallback: try to extract filename from path
    file_name = PurePosixPath(path).name if path else ""
    return {
        "workload": "Unknown",
        "user_id": "",
        "file_name": file_name,
        "relative_path": path,
        "site_url": site_url,
        "site_name": "",
    }


def build_job(row: dict, idx: int) -> dict:
    """Build a QueueJob-compatible dict from a CSV row."""
    object_id = row["properties.RawEventData.ObjectId"]
    sharing_scope = row["properties.RawEventData.SharingLinkScope"]
    unique_id = row["properties.RawEventData.UniqueSharingId"]

    parsed = parse_object_id(object_id)

    return {
        "event_id": unique_id,
        "operation": "SharingLinkCreated",
        "workload": parsed["workload"],
        "user_id": parsed["user_id"],
        "object_id": object_id,
        "site_url": parsed["site_url"],
        "file_name": parsed["file_name"],
        "relative_path": parsed["relative_path"],
        "item_type": "File",
        "sharing_type": sharing_scope,
        "sharing_scope": sharing_scope,
        "sharing_permission": "View",
        "event_time": None,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": {},
    }


def get_extension(filename: str) -> str:
    """Get lowercase extension including the dot."""
    p = PurePosixPath(filename.lower())
    return p.suffix if p.suffix else ""


def load_csv() -> list[dict]:
    """Load and return all rows from test_data.csv."""
    rows = []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def dry_run(rows: list[dict], skip_excluded: bool) -> None:
    """Parse all rows and print summary statistics."""
    jobs = [build_job(r, i) for i, r in enumerate(rows)]

    ext_counter: Counter = Counter()
    workload_counter: Counter = Counter()
    excluded_count = 0

    for j in jobs:
        ext = get_extension(j["file_name"])
        ext_counter[ext or "(none)"] += 1
        workload_counter[j["workload"]] += 1
        if ext in EXCLUDED_EXTENSIONS:
            excluded_count += 1

    processable = len(jobs) - excluded_count

    print(f"\nTotal rows: {len(rows)}")
    print(f"Processable: {processable}")
    print(f"Excluded (video/audio/binary/etc.): {excluded_count}")
    if skip_excluded:
        print(f"After --skip-excluded filter: {processable} rows")

    print(f"\nWorkload distribution:")
    for wl, count in workload_counter.most_common():
        print(f"  {wl}: {count}")

    print(f"\nFile type distribution (top 20):")
    for ext, count in ext_counter.most_common(20):
        excluded_mark = " [EXCLUDED]" if ext in EXCLUDED_EXTENSIONS else ""
        print(f"  {ext}: {count}{excluded_mark}")

    # Sample jobs
    print(f"\nSample jobs (first 3):")
    for j in jobs[:3]:
        print(f"  [{j['workload']}] {j['user_id'] or j.get('site_name', '')} -> {j['file_name']}")


async def process_single(rows: list[dict], row_idx: int) -> None:
    """Process a single row through the full pipeline."""
    if row_idx < 0 or row_idx >= len(rows):
        print(f"Error: row index {row_idx} out of range (0-{len(rows)-1})")
        sys.exit(1)

    job_data = build_job(rows[row_idx], row_idx)
    print(f"Processing row {row_idx}: {job_data['file_name']}")
    print(f"  Object ID: {job_data['object_id']}")
    print(f"  Workload: {job_data['workload']}")
    print(f"  User: {job_data['user_id']}")

    # Import worker modules
    import asyncpg

    from app.ai.anthropic_provider import AnthropicProvider
    from app.ai.gemini_provider import GeminiProvider
    from app.ai.openai_provider import OpenAIProvider
    from app.config import Config
    from app.notifications.base_notifier import NotificationDispatcher
    from app.pipeline.orchestrator import process_job

    config = Config.from_env()
    config.tmpfs_path = str(PROJECT_ROOT / "tmp_test")
    os.makedirs(config.tmpfs_path, exist_ok=True)

    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)

    # Build AI provider
    if config.ai_provider == "anthropic":
        ai = AnthropicProvider(api_key=config.anthropic_api_key, model=config.anthropic_model,
                               max_tokens=config.ai_max_tokens, temperature=config.ai_temperature)
    elif config.ai_provider == "openai":
        ai = OpenAIProvider(api_key=config.openai_api_key, model=config.openai_model,
                            max_tokens=config.ai_max_tokens, temperature=config.ai_temperature)
    elif config.ai_provider == "gemini":
        ai = GeminiProvider(api_key=config.gemini_api_key, model=config.gemini_model,
                            max_tokens=config.ai_max_tokens, temperature=config.ai_temperature,
                            project=config.vertex_project, location=config.vertex_location)
    else:
        print(f"Unknown AI provider: {config.ai_provider}")
        sys.exit(1)

    notifier = NotificationDispatcher([])

    try:
        await process_job(job_data, config, db_pool, None, ai, notifier)
        print(f"\n  Pipeline completed for row {row_idx}")
    except Exception as e:
        print(f"\n  Pipeline FAILED: {e}")
    finally:
        await db_pool.close()


async def batch_process(rows: list[dict], limit: int | None, skip_excluded: bool) -> None:
    """Process multiple rows sequentially."""
    import asyncpg

    from app.ai.anthropic_provider import AnthropicProvider
    from app.ai.gemini_provider import GeminiProvider
    from app.ai.openai_provider import OpenAIProvider
    from app.config import Config
    from app.notifications.base_notifier import NotificationDispatcher
    from app.pipeline.orchestrator import process_job

    config = Config.from_env()
    config.tmpfs_path = str(PROJECT_ROOT / "tmp_test")
    os.makedirs(config.tmpfs_path, exist_ok=True)

    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)

    if config.ai_provider == "anthropic":
        ai = AnthropicProvider(api_key=config.anthropic_api_key, model=config.anthropic_model,
                               max_tokens=config.ai_max_tokens, temperature=config.ai_temperature)
    elif config.ai_provider == "openai":
        ai = OpenAIProvider(api_key=config.openai_api_key, model=config.openai_model,
                            max_tokens=config.ai_max_tokens, temperature=config.ai_temperature)
    elif config.ai_provider == "gemini":
        ai = GeminiProvider(api_key=config.gemini_api_key, model=config.gemini_model,
                            max_tokens=config.ai_max_tokens, temperature=config.ai_temperature,
                            project=config.vertex_project, location=config.vertex_location)
    else:
        print(f"Unknown AI provider: {config.ai_provider}")
        sys.exit(1)

    notifier = NotificationDispatcher([])

    jobs = [build_job(r, i) for i, r in enumerate(rows)]
    if skip_excluded:
        jobs = [j for j in jobs if get_extension(j["file_name"]) not in EXCLUDED_EXTENSIONS]

    if limit:
        jobs = jobs[:limit]

    total = len(jobs)
    success = 0
    failed = 0

    try:
        for i, job_data in enumerate(jobs):
            print(f"[{i+1}/{total}] Processing: {job_data['file_name']}")
            try:
                await process_job(job_data, config, db_pool, None, ai, notifier)
                success += 1
                print(f"  -> OK")
            except Exception as e:
                failed += 1
                print(f"  -> FAIL: {e}")
    finally:
        await db_pool.close()

    print(f"\nBatch complete: {success} OK, {failed} FAIL out of {total}")


async def enqueue_jobs(rows: list[dict], skip_excluded: bool) -> None:
    """Push jobs onto the Redis queue for the worker container."""
    import redis.asyncio as aioredis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)

    jobs = [build_job(row, i) for i, row in enumerate(rows)]
    if skip_excluded:
        jobs = [j for j in jobs if get_extension(j["file_name"]) not in EXCLUDED_EXTENSIONS]

    queue_name = "sharesentinel:jobs"
    count = 0
    for job_data in jobs:
        await r.rpush(queue_name, json.dumps(job_data))
        count += 1

    await r.aclose()
    print(f"Enqueued {count} jobs to Redis queue '{queue_name}'")


def main():
    parser = argparse.ArgumentParser(description="ShareSentinel CSV test runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Parse and summarize without processing")
    group.add_argument("--single", type=int, metavar="N", help="Process row N through the full pipeline")
    group.add_argument("--batch", action="store_true", help="Process all rows sequentially")
    group.add_argument("--enqueue", action="store_true", help="Push jobs onto Redis queue")
    parser.add_argument("--limit", type=int, help="Limit number of rows for --batch")
    parser.add_argument("--skip-excluded", action="store_true", help="Skip excluded file types (video/audio/binary)")
    args = parser.parse_args()

    if not CSV_FILE.exists():
        print(f"Error: {CSV_FILE} not found")
        sys.exit(1)

    rows = load_csv()
    print(f"Loaded {len(rows)} rows from {CSV_FILE.name}")

    if args.dry_run:
        dry_run(rows, args.skip_excluded)
    elif args.single is not None:
        asyncio.run(process_single(rows, args.single))
    elif args.batch:
        asyncio.run(batch_process(rows, args.limit, args.skip_excluded))
    elif args.enqueue:
        asyncio.run(enqueue_jobs(rows, args.skip_excluded))


if __name__ == "__main__":
    main()
