"""Download the Tier 1/2 flagged OneDrive backfill files for human breach review.

Pulls the real file content of every ``AccessCheckpointBackfill`` event flagged
Tier 1 or Tier 2 (by primary escalation OR by cross-provider second look) to the
persistent, host-mounted review directory (``/app/review_downloads`` ->
``./review_downloads``).

This is a deliberate, operator-requested exception to ShareSentinel's normal
"files never touch persistent disk" rule. The files are sensitive — handle the
output directory accordingly.

Layout:
    review_downloads/
        tier_1/<owner>/<file>
        tier_2/<owner>/<file>
        review_manifest_access_checkpoint.csv

Each flagged event already has drive_id / item_id_graph stored from the metadata
pre-screen, so downloads go straight through GraphClient.download_file.

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.download_tier_review --dry-run
    docker exec sharesentinel-worker python -m scripts.download_tier_review
    docker exec sharesentinel-worker python -m scripts.download_tier_review --concurrency 8
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("download_tier_review")

import asyncpg

from app.config import Config
from app.graph_api.client import (
    GraphClient,
    AccessDeniedError,
    FileNotFoundError as GraphFileNotFoundError,
    GraphAPIError,
)
from scripts.backfill_public_sites import derive_file_name
from scripts.download_for_review import (
    _build_graph_auth, site_label, safe_name, unique_path, _cats,
)

TIERS = ("tier_1", "tier_2")
OUTPUT_ROOT = "/app/review_downloads"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download Tier 1/2 flagged OneDrive files for review")
    p.add_argument("--operation", default="AccessCheckpointBackfill")
    p.add_argument("--out", default=OUTPUT_ROOT, help="Output root directory")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve targets + sizes and report, but download nothing")
    p.add_argument("--limit", type=int, default=0, help="Max files to download (0 = all)")
    p.add_argument("--concurrency", type=int, default=8, help="Concurrent downloads (default 8)")
    p.add_argument("--skip-existing", action="store_true",
                   help="Resume: skip a target whose base dest file already exists non-empty")
    return p.parse_args()


def primary_tier(rec: dict) -> str:
    return rec["escalation_tier"] if rec["escalation_tier"] in TIERS else rec["second_look_tier"]


async def load_flagged(pool: asyncpg.Pool, operation: str) -> list[dict]:
    query = """
        SELECT e.object_id, e.file_name, e.drive_id, e.item_id_graph,
               e.file_size_bytes, e.web_url,
               v.escalation_tier, v.second_look_tier, v.risk_score,
               v.categories_detected, v.summary
        FROM events e JOIN verdicts v ON v.event_id = e.event_id
        WHERE e.operation = $1
          AND (v.escalation_tier = ANY($2::text[]) OR v.second_look_tier = ANY($2::text[]))
          AND e.drive_id <> '' AND e.item_id_graph <> ''
        ORDER BY (v.escalation_tier = 'tier_1' OR v.second_look_tier = 'tier_1') DESC,
                 v.risk_score DESC NULLS LAST
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, operation, list(TIERS))
    return [dict(r) for r in rows]


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    out_root = Path(args.out)
    graph = GraphClient(auth=_build_graph_auth(config))

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    flagged = await load_flagged(pool, args.operation)
    await pool.close()
    if args.limit > 0:
        flagged = flagged[: args.limit]
    logger.info("Flagged Tier 1/2 files to download: %d", len(flagged))

    if args.dry_run:
        total = sum((r["file_size_bytes"] or 0) for r in flagged)
        t1 = sum(1 for r in flagged if primary_tier(r) == "tier_1")
        print(f"\n[dry-run] Would download {len(flagged)} files "
              f"({t1} tier_1, {len(flagged) - t1} tier_2), "
              f"~{total / 1024**3:.2f} GB, into {out_root}")
        for r in flagged[:15]:
            print(f"  [{primary_tier(r)}] {site_label(r['object_id'])}/"
                  f"{r['file_name'] or derive_file_name(r['object_id'])}")
        if len(flagged) > 15:
            print(f"  ... and {len(flagged) - 15} more")
        return

    out_root.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    manifest_rows: list[dict] = []
    counters = {"downloaded": 0, "errors": 0, "bytes": 0, "skipped": 0}
    mlock = asyncio.Lock()

    async def fetch(rec: dict) -> None:
        tier = primary_tier(rec)
        owner = site_label(rec["object_id"])
        file_name = rec["file_name"] or derive_file_name(rec["object_id"])
        dest_dir = out_root / tier / owner
        if args.skip_existing:
            base = dest_dir / safe_name(file_name)
            if base.exists() and base.stat().st_size > 0:
                # Keep the manifest complete by re-recording the existing file.
                async with mlock:
                    counters["skipped"] += 1
                    counters["bytes"] += base.stat().st_size
                    manifest_rows.append({
                        "tier": tier, "owner": owner, "file_name": file_name,
                        "local_path": str(base), "size_bytes": base.stat().st_size,
                        "risk_score": rec["risk_score"],
                        "categories": _cats(rec["categories_detected"]),
                        "summary": (rec["summary"] or "").replace("\n", " ").strip(),
                        "status": "skipped_existing", "object_url": rec["object_id"],
                        "web_url": rec["web_url"] or "",
                    })
                return
        async with sem:
            # mkdir + unique_path under a lock to avoid races on the same dir
            async with mlock:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_path(dest_dir, file_name)
                # touch a placeholder so concurrent unique_path calls don't collide
                dest.touch()
            try:
                await graph.download_file(rec["drive_id"], rec["item_id_graph"], dest)
                real_size = dest.stat().st_size
                status = "downloaded"
                async with mlock:
                    counters["downloaded"] += 1
                    counters["bytes"] += real_size
                logger.info("[%s] %s/%s (%d bytes)", tier, owner, dest.name, real_size)
            except Exception as exc:
                # Catch-all (incl. transient httpx.ReadError/ConnectError, Graph
                # errors, OSError) so a single failure can never abort the batch.
                real_size = 0
                status = f"error:{type(exc).__name__}"
                async with mlock:
                    counters["errors"] += 1
                # remove the empty/partial placeholder on failure
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.warning("[%s] FAILED %s/%s: %s", tier, owner, file_name, exc)
            async with mlock:
                manifest_rows.append({
                    "tier": tier, "owner": owner, "file_name": file_name,
                    "local_path": str(dest), "size_bytes": real_size,
                    "risk_score": rec["risk_score"], "categories": _cats(rec["categories_detected"]),
                    "summary": (rec["summary"] or "").replace("\n", " ").strip(),
                    "status": status, "object_url": rec["object_id"],
                    "web_url": rec["web_url"] or "",
                })

    await asyncio.gather(*(fetch(r) for r in flagged))

    # ── Write manifest ───────────────────────────────────────────────
    manifest_path = out_root / "review_manifest_access_checkpoint.csv"
    cols = ["tier", "owner", "file_name", "local_path", "size_bytes", "risk_score",
            "categories", "summary", "status", "object_url", "web_url"]
    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in sorted(manifest_rows, key=lambda r: (r["tier"], r["owner"], r["file_name"])):
            w.writerow({c: row.get(c, "") for c in cols})

    gb = counters["bytes"] / (1024 ** 3)
    print()
    print("=" * 56)
    print("  Tier 1/2 Review Download complete")
    print("=" * 56)
    print(f"  Targets:            {len(flagged):>8,}")
    print(f"  Downloaded:         {counters['downloaded']:>8,}")
    print(f"  Skipped (existing): {counters['skipped']:>8,}")
    print(f"  Errors:             {counters['errors']:>8,}")
    print(f"  Total size:         {gb:>8.2f} GB")
    print(f"  Output root:        {out_root}")
    print(f"  Manifest:           {manifest_path}")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
