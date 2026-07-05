"""Export AI analysis results for the formerly-public-sites backfill.

Joins every object in the original ``public-sites_unique_objects.csv`` to its
ShareSentinel event + AI verdict and writes an enriched CSV containing the AI
summary, risk score, detected sensitivity classification, and tier flag.

Rows that were never analysed (site chrome, unprocessable types, items that
404'd, etc.) are still emitted with a status explaining why, so the output is a
1:1 superset of the input.

Join strategy: each input object URL is canonicalised exactly as the backfill
script did, then matched to events by ``object_id``. This catches both the
PublicSiteBackfill events and any pre-existing event for the same object. When
an object maps to several events, the one bearing a verdict (most recent) wins.

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.export_public_sites_analysis
    docker cp sharesentinel-worker:/app/public-sites_analysis.csv ./public-sites_analysis.csv
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("export_public_sites_analysis")

import asyncpg

from app.config import Config
from app.pipeline.classifier import FileClassifier
from scripts.backfill_public_sites import (
    ENQUEUE_ACTIONS,
    canonicalize_url,
    derive_file_name,
    is_site_chrome,
)

OUTPUT_HEADER = [
    "ObjectId",
    "UserCount",
    "file_name",
    "analysis_status",
    "flagged",
    "escalation_tier",
    "risk_score",
    "categories_detected",
    "overall_context",
    "data_recency",
    "ai_summary",
    "recommendation",
    "analysis_mode",
    "mime_type",
    "file_size_bytes",
    "ai_provider",
    "ai_model",
    "second_look_agreed",
    "web_url",
    "event_id",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export AI analysis for formerly-public-sites backfill")
    p.add_argument("--csv", default="/app/public-sites_unique_objects.csv",
                   help="Path to the original input CSV")
    p.add_argument("--out", default="/app/public-sites_analysis.csv",
                   help="Path to write the enriched output CSV")
    return p.parse_args()


def _jsonb_to_list_str(value) -> str:
    """Render a jsonb array (asyncpg returns it as a str) into 'a; b; c'."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return str(value)


async def load_event_map(pool: asyncpg.Pool, object_ids: list[str]) -> dict[str, dict]:
    """Return {object_id: best matching event+verdict record}.

    Picks, per object_id, the event whose latest verdict exists (preferring
    verdict-bearing rows), then most recent event.
    """
    query = """
        SELECT
            e.object_id, e.event_id, e.status, e.failure_reason,
            e.mime_type, e.file_size_bytes, e.web_url, e.created_at,
            v.escalation_tier, v.risk_score, v.categories_detected,
            v.overall_context, v.data_recency, v.summary, v.recommendation,
            v.analysis_mode, v.ai_provider, v.ai_model, v.second_look_agreed,
            (v.event_id IS NOT NULL) AS has_verdict
        FROM events e
        LEFT JOIN LATERAL (
            SELECT * FROM verdicts WHERE event_id = e.event_id
            ORDER BY created_at DESC LIMIT 1
        ) v ON TRUE
        WHERE e.object_id = ANY($1::text[])
    """
    result: dict[str, dict] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, object_ids)

    for row in rows:
        oid = row["object_id"]
        existing = result.get(oid)
        if existing is None:
            result[oid] = dict(row)
            continue
        # Prefer a verdict-bearing row; tie-break on most recent event.
        better = (
            (row["has_verdict"], row["created_at"])
            > (existing["has_verdict"], existing["created_at"])
        )
        if better:
            result[oid] = dict(row)
    return result


async def main() -> None:
    args = parse_args()
    if not os.path.exists(args.csv):
        print(f"ERROR: input CSV not found: {args.csv}")
        sys.exit(1)

    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    classifier = FileClassifier()

    # ── Read input, build canonical URL list ─────────────────────────
    input_rows: list[dict] = []
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            input_rows.append(r)
    logger.info("Loaded %d input rows", len(input_rows))

    canon_for_row: list[str] = []
    canon_set: set[str] = set()
    for r in input_rows:
        raw = (r.get("ObjectId") or "").strip()
        canon = canonicalize_url(raw) if raw else ""
        canon_for_row.append(canon)
        if canon:
            canon_set.add(canon)

    # ── Load events+verdicts from DB ─────────────────────────────────
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    logger.info("Querying %d distinct object URLs...", len(canon_set))
    event_map = await load_event_map(pool, list(canon_set))
    await pool.close()
    logger.info("Matched %d objects to events/verdicts", len(event_map))

    # ── Write enriched CSV ───────────────────────────────────────────
    counts = {"analyzed": 0, "flagged": 0, "not_found": 0,
              "skipped_chrome": 0, "skipped_unprocessable": 0, "no_event": 0}

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_HEADER)

        for r, canon in zip(input_rows, canon_for_row):
            object_id = (r.get("ObjectId") or "").strip()
            user_count = (r.get("UserCount") or "").strip()
            file_name = derive_file_name(canon) if canon else ""

            rec = event_map.get(canon)

            if rec is None:
                # Never became an event — explain why using the same logic
                # the backfill applied at enqueue time.
                if not object_id:
                    status = "blank_object_id"
                elif is_site_chrome(canon, file_name):
                    status = "not_analyzed_site_chrome"
                    counts["skipped_chrome"] += 1
                else:
                    action = classifier.classify(file_name, "File", 0, config).action
                    if action not in ENQUEUE_ACTIONS:
                        status = "not_analyzed_unprocessable_type"
                        counts["skipped_unprocessable"] += 1
                    else:
                        status = "not_found_in_db"
                        counts["no_event"] += 1
                writer.writerow([
                    object_id, user_count, file_name, status,
                    "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                ])
                continue

            has_verdict = rec["has_verdict"]
            tier = rec["escalation_tier"] or ""
            flagged = "TRUE" if tier in ("tier_1", "tier_2") else "FALSE"

            # Status string: event status (+ failure reason when present).
            status = rec["status"] or ""
            if rec["failure_reason"]:
                status = f"{status}/{rec['failure_reason']}"

            if has_verdict:
                counts["analyzed"] += 1
                if flagged == "TRUE":
                    counts["flagged"] += 1
            elif rec["failure_reason"] == "file_not_found":
                counts["not_found"] += 1

            writer.writerow([
                object_id,
                user_count,
                file_name,
                status,
                flagged if has_verdict else "",
                tier,
                rec["risk_score"] if rec["risk_score"] is not None else "",
                _jsonb_to_list_str(rec["categories_detected"]),
                rec["overall_context"] or "",
                rec["data_recency"] or "",
                rec["summary"] or "",
                rec["recommendation"] or "",
                rec["analysis_mode"] or "",
                rec["mime_type"] or "",
                rec["file_size_bytes"] if rec["file_size_bytes"] is not None else "",
                rec["ai_provider"] or "",
                rec["ai_model"] or "",
                "" if rec["second_look_agreed"] is None else ("TRUE" if rec["second_look_agreed"] else "FALSE"),
                rec["web_url"] or "",
                rec["event_id"] or "",
            ])

    print()
    print("=" * 56)
    print("  Export complete")
    print("=" * 56)
    print(f"  Output file:                {args.out}")
    print(f"  Input rows:                 {len(input_rows):>8,}")
    print(f"  Analyzed (with verdict):    {counts['analyzed']:>8,}")
    print(f"    of which flagged (T1/T2): {counts['flagged']:>8,}")
    print(f"  File not found (404):       {counts['not_found']:>8,}")
    print(f"  Not analyzed — site chrome: {counts['skipped_chrome']:>8,}")
    print(f"  Not analyzed — unprocess.:  {counts['skipped_unprocessable']:>8,}")
    print(f"  No event in DB:             {counts['no_event']:>8,}")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
