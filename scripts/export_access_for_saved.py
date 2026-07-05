"""Export every access-log event for the files saved during the targeted PII review.

For the legal investigation we saved originals confirmed to contain a full SSN /
driver's license / financial account number. Legal wants the full access history
of those specific files — every recorded access event, not aggregates.

The access-log CSVs are large host-resident raw inputs (not part of the
container data model), so this is a HOST-SIDE, stdlib-only matcher:

  1. Dump the saved files' keys from the DB (run inside the container):
       docker exec sharesentinel-postgres psql -U sharesentinel -d sharesentinel -c "\\copy (
         SELECT t.event_id, t.file_name, t.review_status,
                t.full_ssn_found, t.full_dl_number_found, t.full_dl_image_found, t.full_financial_found,
                e.object_id, e.web_url
         FROM targeted_pii_review t JOIN events e ON e.event_id=t.event_id
         WHERE t.run_id IN ('20260617T201740Z','20260618T013051Z') AND t.saved_path<>''
       ) TO STDOUT WITH CSV HEADER" > /tmp/saved_47_keys.csv

  2. Run this matcher on the host:
       python3 scripts/export_access_for_saved.py \\
         --keys /tmp/saved_47_keys.csv \\
         --csv access_checkpoint_part1.csv access_checkpoint_part2.csv \\
         --out review_downloads/access_events_for_saved_files.csv

Files are matched by canonicalised ObjectId (replicating the exact
canonicalisation scripts/backfill_access_checkpoint.py used so keys align with
the stored object_id), with a FlaggedWebUrl fallback.
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote

csv.field_size_limit(10 * 1024 * 1024)

# Verbatim copy of scripts/backfill_access_checkpoint.canonicalize_url — kept in
# sync so the match keys line up with the stored, already-canonical object_id.
def canonicalize_url(raw: str) -> str:
    parts = urlsplit(raw.strip())
    encoded_path = quote(parts.path, safe="/%()!*'-._~&$+=@")
    return urlunsplit((parts.scheme, parts.netloc, encoded_path, parts.query, parts.fragment))


ACCESS_COLS = [
    "Source", "ObjectId", "MatchedTarget", "Operation", "AccessTimestampUTC",
    "AccessedByUPN", "UserType", "IsInternal", "ClientIP", "Service", "Tier",
    "DetectedCategories", "FileName", "FlaggedWebUrl", "ShareType", "SharedWith", "Site",
]
LABEL_COLS = [
    "saved_file_name", "saved_event_id", "found_ssn", "found_dl_number",
    "found_dl_image", "found_financial", "review_status",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export access events for the saved targeted-PII files")
    p.add_argument("--keys", required=True, help="CSV of saved-file keys dumped from the DB")
    p.add_argument("--csv", nargs="+", required=True, help="Access-log CSV path(s)")
    p.add_argument("--out", default="review_downloads/access_events_for_saved_files.csv",
                   help="Output CSV path")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Build match lookups from the dumped keys.
    by_obj: dict[str, dict] = {}
    by_weburl: dict[str, dict] = {}
    saved: list[dict] = []
    with open(args.keys, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            saved.append(r)
            meta = {
                "saved_file_name": r["file_name"],
                "saved_event_id": r["event_id"],
                "found_ssn": r["full_ssn_found"],
                "found_dl_number": r["full_dl_number_found"],
                "found_dl_image": r["full_dl_image_found"],
                "found_financial": r["full_financial_found"],
                "review_status": r["review_status"],
            }
            if r.get("object_id"):
                by_obj[canonicalize_url(r["object_id"])] = meta
            if r.get("web_url"):
                by_weburl[r["web_url"].strip()] = meta
    print(f"Saved files to trace access for: {len(saved)}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    per_file_events: dict[str, int] = defaultdict(int)
    total_events = 0
    rows_scanned = 0

    with open(out_path, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=LABEL_COLS + ACCESS_COLS)
        writer.writeheader()
        for path in args.csv:
            p = Path(path)
            if not p.exists():
                print(f"WARNING: CSV not found, skipping: {path}")
                continue
            print(f"Scanning {path} ...")
            with open(p, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    rows_scanned += 1
                    obj = (row.get("ObjectId") or "").strip()
                    meta = by_obj.get(canonicalize_url(obj)) if obj else None
                    if meta is None:
                        meta = by_weburl.get((row.get("FlaggedWebUrl") or "").strip())
                    if meta is None:
                        continue
                    out_row = dict(meta)
                    for c in ACCESS_COLS:
                        out_row[c] = row.get(c, "")
                    writer.writerow(out_row)
                    total_events += 1
                    per_file_events[meta["saved_event_id"]] += 1

    matched_files = len(per_file_events)
    no_access = [r for r in saved if per_file_events.get(r["event_id"], 0) == 0]

    print("\n" + "=" * 64)
    print("  Access-event export for saved targeted-PII files")
    print("=" * 64)
    print(f"  Saved files:             {len(saved):>8,}")
    print(f"  Files with access rows:  {matched_files:>8,}")
    print(f"  Files with NO access:    {len(no_access):>8,}")
    print(f"  Access CSV rows scanned: {rows_scanned:>9,}")
    print(f"  Access events exported:  {total_events:>8,}")
    print(f"  Output CSV:              {out_path}")
    print("=" * 64)
    if no_access:
        print("  Files with no access rows in the provided logs:")
        for r in sorted(no_access, key=lambda x: x["file_name"]):
            print(f"    - {r['file_name']}")
    print()


if __name__ == "__main__":
    main()
