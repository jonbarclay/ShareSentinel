"""Export Tier 1 / Tier 2 flagged OneDrive backfill files for human breach review.

Joins the AI verdicts for ``operation='AccessCheckpointBackfill'`` events
(Tier 1 / Tier 2 by primary escalation OR by cross-provider second look) to
per-file access statistics computed from the source access-log CSVs.

The access enrichment is the point: for each flagged file we report WHO touched
it, bucketed by accessor type, so a reviewer can judge breach-notification risk:
  * internal        — real @uvu.edu users, IsInternal=True
  * guest_external  — B2B guests (``#ext#`` UPNs)
  * anonymous       — anonymous-link access (``urn:spo:anon#``)
  * tenant_anon     — org-wide link access (``urn:spo:tenantanon#``)
  * service         — SharePoint service account (``app@sharepoint`` / Application)

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.export_tier_review \
        --csv /app/access_checkpoint_part1.csv /app/access_checkpoint_part2.csv \
        --out /app/onedrive_tier1_tier2_breach_review_20260608.csv
Then copy it out:
    docker cp sharesentinel-worker:/app/onedrive_tier1_tier2_breach_review_20260608.csv .
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlsplit, unquote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("export_tier_review")

import asyncpg

from app.config import Config
# Reuse the exact canonicalisation the backfill used so CSV ObjectIds match the
# percent-encoded object_id stored in the events table.
from scripts.backfill_access_checkpoint import canonicalize_url

csv.field_size_limit(10 * 1024 * 1024)

TIERS = ("tier_1", "tier_2")
_EXTERNAL_BUCKETS = ("guest_external", "anonymous", "tenant_anon", "other_external")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export Tier 1/2 flagged files for breach review")
    p.add_argument("--csv", nargs="+", required=True,
                   help="Access-log CSV path(s) to source access stats from")
    p.add_argument("--out", default="/app/onedrive_tier1_tier2_breach_review.csv",
                   help="Output CSV path (inside container)")
    p.add_argument("--operation", default="AccessCheckpointBackfill")
    return p.parse_args()


def classify_accessor(upn: str, user_type: str, is_internal: str) -> str:
    u = (upn or "").strip().lower()
    if u.startswith("urn:spo:anon#"):
        return "anonymous"
    if u.startswith("urn:spo:tenantanon#"):
        return "tenant_anon"
    if "#ext#" in u:
        return "guest_external"
    if u == "app@sharepoint" or (user_type or "").strip().lower() == "application":
        return "service"
    if (is_internal or "").strip().lower() == "true" or u.endswith("@uvu.edu"):
        return "internal"
    if not u:
        return "service"
    return "other_external"


def owner_from_url(object_url: str) -> tuple[str, str]:
    """Return (owner_personal_segment, best-effort owner UPN) for a OneDrive URL."""
    segs = [s for s in urlsplit(object_url).path.split("/") if s]
    if len(segs) >= 2 and segs[0].lower() == "personal":
        seg = segs[1]  # e.g. 10504797_uvu_edu
        guess = seg[:-len("_uvu_edu")] + "@uvu.edu" if seg.lower().endswith("_uvu_edu") else seg
        return seg, guess
    return "", ""


def jlist(val) -> str:
    """Flatten a jsonb list/string to a comma-joined string."""
    if val is None:
        return ""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (ValueError, TypeError):
            return val
    if isinstance(val, list):
        return ", ".join(str(x) for x in val)
    return str(val)


def parse_ts(raw: str):
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    # ── 1. Fetch flagged verdicts ────────────────────────────────────
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.event_id, e.object_id, e.file_name, e.site_url, e.raw_payload,
                   v.id AS verdict_id, v.escalation_tier, v.second_look_tier,
                   v.second_look_agreed, v.risk_score, v.confidence,
                   v.categories_detected, v.second_look_categories, v.pii_types_found,
                   v.affected_count, v.summary, v.recommendation,
                   v.ai_provider, v.ai_model, v.created_at
            FROM verdicts v
            JOIN events e ON e.event_id = v.event_id
            WHERE e.operation = $1
              AND (v.escalation_tier = ANY($2::text[])
                   OR v.second_look_tier = ANY($2::text[]))
            """,
            args.operation, list(TIERS),
        )
    await pool.close()
    logger.info("Fetched %d flagged verdicts", len(rows))

    # Map canonical object_id -> record. (object_id is already canonical.)
    flagged: dict[str, dict] = {}
    for r in rows:
        flagged[r["object_id"]] = dict(r)
    flagged_keys = set(flagged.keys())

    # ── 2. Accumulate access stats from CSVs for flagged objects only ─
    # accessors[obj][bucket] = set of identities; events[obj] = total rows
    accessors: dict[str, dict] = defaultdict(
        lambda: {b: set() for b in ("internal",) + _EXTERNAL_BUCKETS + ("service",)}
    )
    ev_total: dict[str, int] = defaultdict(int)
    ts_min: dict[str, datetime] = {}
    ts_max: dict[str, datetime] = {}

    for path in args.csv:
        if not os.path.exists(path):
            logger.warning("CSV not found, skipping: %s", path)
            continue
        logger.info("Scanning %s ...", path)
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_obj = (row.get("ObjectId") or "").strip()
                if not raw_obj:
                    continue
                key = canonicalize_url(raw_obj)
                if key not in flagged_keys:
                    continue
                ev_total[key] += 1
                bucket = classify_accessor(
                    row.get("AccessedByUPN", ""), row.get("UserType", ""),
                    row.get("IsInternal", ""),
                )
                accessors[key][bucket].add((row.get("AccessedByUPN") or "").strip())
                ts = parse_ts(row.get("AccessTimestampUTC", ""))
                if ts:
                    if key not in ts_min or ts < ts_min[key]:
                        ts_min[key] = ts
                    if key not in ts_max or ts > ts_max[key]:
                        ts_max[key] = ts

    # ── 3. Build output rows ─────────────────────────────────────────
    def primary_tier(rec: dict) -> str:
        if rec["escalation_tier"] in TIERS:
            return rec["escalation_tier"]
        return rec["second_look_tier"]

    def flagged_by(rec: dict) -> str:
        p = rec["escalation_tier"] in TIERS
        s = rec["second_look_tier"] in TIERS
        return "both" if p and s else ("primary" if p else "second_look")

    out_rows = []
    for obj, rec in flagged.items():
        acc = accessors.get(obj)
        rp = rec.get("raw_payload") or {}
        if isinstance(rp, str):
            try:
                rp = json.loads(rp)
            except (ValueError, TypeError):
                rp = {}
        owner_seg, owner_upn = owner_from_url(obj)

        def n(bucket):
            return len(acc[bucket]) if acc else 0

        ext_total = sum(n(b) for b in _EXTERNAL_BUCKETS)
        guest_list = sorted(acc["guest_external"]) if acc else []

        out_rows.append({
            "tier": primary_tier(rec),
            "flagged_by": flagged_by(rec),
            "escalation_tier": rec["escalation_tier"] or "",
            "second_look_tier": rec["second_look_tier"] or "",
            "second_look_agreed": rec["second_look_agreed"],
            "risk_score": rec["risk_score"],
            "confidence": rec["confidence"] or "",
            "categories_detected": jlist(rec["categories_detected"]),
            "second_look_categories": jlist(rec["second_look_categories"]),
            "pii_types_found": jlist(rec["pii_types_found"]),
            "affected_count": rec["affected_count"],
            "summary": (rec["summary"] or "").replace("\n", " ").strip(),
            "recommendation": (rec["recommendation"] or "").replace("\n", " ").strip(),
            "file_name": rec["file_name"] or "",
            "owner_upn_guess": owner_upn,
            "owner_personal_path": owner_seg,
            "object_url": obj,
            "share_type_raw": rp.get("share_type_raw", ""),
            "shared_with_raw": rp.get("shared_with_raw", ""),
            "source_csv": rp.get("csv_file", ""),
            # access enrichment
            "access_events_total": ev_total.get(obj, 0),
            "distinct_internal_users": n("internal"),
            "distinct_guest_external": n("guest_external"),
            "distinct_anonymous": n("anonymous"),
            "distinct_tenant_anon": n("tenant_anon"),
            "distinct_other_external": n("other_external"),
            "distinct_service": n("service"),
            "any_external_or_anon_access": "YES" if ext_total > 0 else "no",
            "guest_external_upns": "; ".join(guest_list[:25]),
            "first_access_utc": ts_min[obj].isoformat() if obj in ts_min else "",
            "last_access_utc": ts_max[obj].isoformat() if obj in ts_max else "",
            "ai_provider": rec["ai_provider"] or "",
            "ai_model": rec["ai_model"] or "",
            "analyzed_at": rec["created_at"].isoformat() if rec["created_at"] else "",
            "event_id": rec["event_id"],
            "verdict_id": rec["verdict_id"],
        })

    # Sort: Tier 1 before Tier 2, then external-access first, then risk, affected.
    tier_rank = {"tier_1": 0, "tier_2": 1}
    out_rows.sort(key=lambda r: (
        tier_rank.get(r["tier"], 9),
        0 if r["any_external_or_anon_access"] == "YES" else 1,
        -(r["risk_score"] or 0),
        -(r["affected_count"] or 0),
    ))

    # ── 4. Write CSV ─────────────────────────────────────────────────
    fieldnames = list(out_rows[0].keys()) if out_rows else []
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    t1 = sum(1 for r in out_rows if r["tier"] == "tier_1")
    t2 = sum(1 for r in out_rows if r["tier"] == "tier_2")
    ext = sum(1 for r in out_rows if r["any_external_or_anon_access"] == "YES")
    print()
    print("=" * 60)
    print("  Tier 1/2 Breach-Review Export")
    print("=" * 60)
    print(f"  Flagged files exported:   {len(out_rows):>6,}")
    print(f"    Tier 1:                 {t1:>6,}")
    print(f"    Tier 2:                 {t2:>6,}")
    print(f"  With external/anon access:{ext:>6,}")
    print(f"  Output:                   {args.out}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
