"""Download flagged + video files from the formerly-public-sites set for human review.

A human needs to inspect the actual content of over-shared sensitive files and
decide what remediation is warranted. This script downloads, to a persistent
host directory (mounted at /app/review_downloads), the real file content for:

  * every PublicSiteBackfill file flagged Tier 1 or Tier 2, and
  * every video file from the original CSV (videos are never auto-analysed).

This is a deliberate, operator-requested exception to ShareSentinel's normal
"files never touch persistent disk" rule. The files are sensitive — handle the
output directory accordingly.

Layout:
    review_downloads/
        tier_1/<Site>/<file>
        tier_2/<Site>/<file>
        video/<Site>/<file>
        review_manifest.csv     # one row per file with the AI context

Flagged files reuse the drive_id/item_id already stored on their events. Video
files are resolved live via the Graph /shares endpoint (they have no event).

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.download_for_review --dry-run
    docker exec sharesentinel-worker python -m scripts.download_for_review --videos-only --dry-run
    docker exec sharesentinel-worker python -m scripts.download_for_review
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("download_for_review")

import asyncpg

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.client import (
    GraphClient,
    AccessDeniedError,
    FileNotFoundError as GraphFileNotFoundError,
    GraphAPIError,
)
from scripts.backfill_public_sites import canonicalize_url, derive_file_name

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}
OUTPUT_ROOT = "/app/review_downloads"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download flagged + video files for human review")
    p.add_argument("--csv", default="/app/public-sites_unique_objects.csv",
                   help="Original input CSV (source of video objects)")
    p.add_argument("--out", default=OUTPUT_ROOT, help="Output root directory")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve targets + sizes and report, but download nothing")
    p.add_argument("--flagged-only", action="store_true", help="Only flagged Tier 1/2 files")
    p.add_argument("--videos-only", action="store_true", help="Only video files")
    p.add_argument("--max-video-mb", type=int, default=0,
                   help="Skip videos larger than this many MB (0 = no cap)")
    return p.parse_args()


def _build_graph_auth(config: Config) -> GraphAuth:
    return GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )


def site_label(url: str) -> str:
    """Short, filesystem-safe site label from an object URL (e.g. '2450Group4')."""
    m = re.search(r"/(?:sites|teams|personal)/([^/]+)", url)
    label = m.group(1) if m else "unknown-site"
    return re.sub(r"[^A-Za-z0-9._-]", "_", label) or "unknown-site"


def safe_name(name: str) -> str:
    """Sanitise a file name for the local filesystem (drop path separators)."""
    name = name.replace("/", "_").replace("\\", "_").strip()
    return name or "unnamed"


def unique_path(directory: Path, file_name: str) -> Path:
    """Return a non-colliding path inside *directory* for *file_name*."""
    candidate = directory / safe_name(file_name)
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(candidate.name)
    i = 2
    while True:
        alt = directory / f"{stem} ({i}){ext}"
        if not alt.exists():
            return alt
        i += 1


async def load_flagged(pool: asyncpg.Pool) -> list[dict]:
    """Flagged Tier 1/2 PublicSiteBackfill files with stored drive/item IDs."""
    query = """
        SELECT e.object_id, e.file_name, e.drive_id, e.item_id_graph,
               e.file_size_bytes, e.web_url,
               v.escalation_tier, v.risk_score, v.categories_detected, v.summary
        FROM events e JOIN verdicts v ON v.event_id = e.event_id
        WHERE e.operation = 'PublicSiteBackfill'
          AND v.escalation_tier IN ('tier_1','tier_2')
          AND e.drive_id <> '' AND e.item_id_graph <> ''
        ORDER BY v.escalation_tier, v.risk_score DESC NULLS LAST
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query)
    return [dict(r) for r in rows]


def load_video_urls(csv_path: str) -> list[str]:
    """Canonical URLs of all video objects in the original CSV."""
    urls: list[str] = []
    seen: set[str] = set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = (row.get("ObjectId") or "").strip()
            if not raw:
                continue
            ext = os.path.splitext(raw)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            canon = canonicalize_url(raw)
            if canon not in seen:
                seen.add(canon)
                urls.append(canon)
    return urls


def _cats(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    return "; ".join(str(v) for v in value) if isinstance(value, list) else str(value)


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    do_flagged = not args.videos_only
    do_videos = not args.flagged_only

    out_root = Path(args.out)
    auth = _build_graph_auth(config)
    graph = GraphClient(auth=auth)

    manifest_rows: list[dict] = []
    downloaded = 0
    skipped = 0
    errors = 0
    total_bytes = 0

    # ── Flagged Tier 1/2 files ───────────────────────────────────────
    flagged: list[dict] = []
    if do_flagged:
        pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
        flagged = await load_flagged(pool)
        await pool.close()
        logger.info("Flagged Tier 1/2 files to download: %d", len(flagged))

    for rec in flagged:
        tier = rec["escalation_tier"]
        site = site_label(rec["object_id"])
        dest_dir = out_root / tier / site
        file_name = rec["file_name"] or derive_file_name(rec["object_id"])
        size = rec["file_size_bytes"] or 0

        if args.dry_run:
            total_bytes += size
            manifest_rows.append({
                "category": tier, "site": site, "file_name": file_name,
                "local_path": str(dest_dir / safe_name(file_name)),
                "size_bytes": size, "risk_score": rec["risk_score"],
                "categories": _cats(rec["categories_detected"]),
                "summary": rec["summary"] or "", "object_id": rec["object_id"],
                "web_url": rec["web_url"] or "", "status": "planned",
            })
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(dest_dir, file_name)
        try:
            await graph.download_file(rec["drive_id"], rec["item_id_graph"], dest)
            real_size = dest.stat().st_size
            total_bytes += real_size
            downloaded += 1
            status = "downloaded"
            logger.info("[%s] %s/%s (%d bytes)", tier, site, dest.name, real_size)
        except (GraphFileNotFoundError, AccessDeniedError, GraphAPIError) as exc:
            errors += 1
            status = f"error:{type(exc).__name__}"
            real_size = 0
            logger.warning("[%s] FAILED %s/%s: %s", tier, site, file_name, exc)
        manifest_rows.append({
            "category": tier, "site": site, "file_name": file_name,
            "local_path": str(dest), "size_bytes": real_size,
            "risk_score": rec["risk_score"], "categories": _cats(rec["categories_detected"]),
            "summary": rec["summary"] or "", "object_id": rec["object_id"],
            "web_url": rec["web_url"] or "", "status": status,
        })

    # ── Video files (resolve live, then download) ────────────────────
    video_urls: list[str] = []
    if do_videos:
        video_urls = load_video_urls(args.csv)
        logger.info("Video objects to resolve: %d", len(video_urls))

    max_video_bytes = args.max_video_mb * 1024 * 1024 if args.max_video_mb else 0

    for url in video_urls:
        site = site_label(url)
        file_name = derive_file_name(url)
        dest_dir = out_root / "video" / site

        # Resolve drive_id / item_id / size via /shares
        try:
            meta = await graph.get_item_metadata(object_id=url)
        except (GraphFileNotFoundError, AccessDeniedError, GraphAPIError) as exc:
            errors += 1
            logger.warning("[video] resolve FAILED %s/%s: %s", site, file_name, exc)
            manifest_rows.append({
                "category": "video", "site": site, "file_name": file_name,
                "local_path": "", "size_bytes": 0, "risk_score": "",
                "categories": "", "summary": "", "object_id": url,
                "web_url": "", "status": f"resolve_error:{type(exc).__name__}",
            })
            continue

        parent = meta.get("parentReference") or {}
        drive_id = parent.get("driveId", "")
        item_id = meta.get("id", "")
        size = meta.get("size", 0) or 0
        web_url = meta.get("webUrl", "")

        if max_video_bytes and size > max_video_bytes:
            skipped += 1
            logger.info("[video] SKIP (too large %d MB) %s/%s", size // 1024 // 1024, site, file_name)
            manifest_rows.append({
                "category": "video", "site": site, "file_name": file_name,
                "local_path": "", "size_bytes": size, "risk_score": "",
                "categories": "", "summary": "", "object_id": url,
                "web_url": web_url, "status": "skipped_too_large",
            })
            continue

        if args.dry_run:
            total_bytes += size
            manifest_rows.append({
                "category": "video", "site": site, "file_name": file_name,
                "local_path": str(dest_dir / safe_name(file_name)), "size_bytes": size,
                "risk_score": "", "categories": "", "summary": "",
                "object_id": url, "web_url": web_url, "status": "planned",
            })
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(dest_dir, file_name)
        try:
            await graph.download_file(drive_id, item_id, dest)
            real_size = dest.stat().st_size
            total_bytes += real_size
            downloaded += 1
            status = "downloaded"
            logger.info("[video] %s/%s (%d MB)", site, dest.name, real_size // 1024 // 1024)
        except (GraphFileNotFoundError, AccessDeniedError, GraphAPIError) as exc:
            errors += 1
            status = f"error:{type(exc).__name__}"
            real_size = 0
            logger.warning("[video] download FAILED %s/%s: %s", site, file_name, exc)
        manifest_rows.append({
            "category": "video", "site": site, "file_name": file_name,
            "local_path": str(dest), "size_bytes": real_size, "risk_score": "",
            "categories": "", "summary": "", "object_id": url,
            "web_url": web_url, "status": status,
        })

    # ── Write manifest ───────────────────────────────────────────────
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / ("review_manifest_dryrun.csv" if args.dry_run else "review_manifest.csv")
    if not args.dry_run or out_root.exists():
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            cols = ["category", "site", "file_name", "local_path", "size_bytes",
                    "risk_score", "categories", "summary", "status", "object_id", "web_url"]
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for row in manifest_rows:
                w.writerow({c: row.get(c, "") for c in cols})

    gb = total_bytes / (1024 ** 3)
    print()
    print("=" * 56)
    print("  Review download " + ("PLAN (dry-run)" if args.dry_run else "complete"))
    print("=" * 56)
    print(f"  Flagged Tier 1/2 targets:   {len(flagged):>8,}")
    print(f"  Video targets resolved:     {len(video_urls):>8,}")
    if args.dry_run:
        print(f"  Planned total size:         {gb:>8.2f} GB")
    else:
        print(f"  Downloaded:                 {downloaded:>8,}")
        print(f"  Skipped (too large):        {skipped:>8,}")
        print(f"  Errors:                     {errors:>8,}")
        print(f"  Total downloaded size:      {gb:>8.2f} GB")
    print(f"  Manifest:                   {manifest_path}")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
