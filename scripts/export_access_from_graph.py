"""Pull access logs for the saved targeted-PII files directly from Microsoft Graph.

Alternative to the CSV-sourced export: query the Graph Audit Logs Query API
(the same async job API the lifecycle-cron audit poller uses) for file-access
operations against the saved files, scoped server-side with objectIdFilters.

IMPORTANT — retention: Graph only returns events still within the tenant's
unified-audit retention (~180 days on Audit Standard). Access events older than
that have aged out and are not retrievable here, so this is a CURRENT-window
view, complementary to the point-in-time CSV export.

Runs inside the worker container (DB + Graph auth + httpx available):
    docker exec sharesentinel-worker python -m scripts.export_access_from_graph \
        --run-id 20260617T201740Z 20260618T013051Z --days 180 \
        --out /app/review_downloads/access_events_from_graph.csv
    # probe one file first to validate objectIdFilters matching:
    docker exec sharesentinel-worker python -m scripts.export_access_from_graph --probe
"""

import argparse
import asyncio
import csv
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("export_access_from_graph")

import asyncpg
import httpx

from app.config import Config
from app.graph_api.auth import GraphAuth
from scripts.backfill_access_checkpoint import canonicalize_url

GRAPH_BETA = "https://graph.microsoft.com/beta"
TIMEOUT = httpx.Timeout(120.0)
POLL_INTERVAL_S = 15
QUERY_TIMEOUT_S = 1800
FILE_ACCESS_OPS = [
    "FileAccessed", "FileDownloaded", "FilePreviewed",
    "FileSyncDownloadedFull", "FileAccessedExtended",
]
RECORD_TYPES = ["sharePointFileOperation"]

OUT_COLS = [
    "saved_file_name", "saved_event_id", "found_ssn", "found_dl_number",
    "found_dl_image", "found_financial", "review_status",
    "operation", "created_utc", "user_principal_name", "user_id", "user_type",
    "is_internal_guess", "client_ip", "workload", "site_url", "object_id_audit",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pull access logs for saved targeted-PII files from Graph")
    p.add_argument("--run-id", nargs="+", default=["20260617T201740Z", "20260618T013051Z"])
    p.add_argument("--days", type=int, default=180, help="Lookback window in days (default 180)")
    p.add_argument("--out", default="/app/review_downloads/access_events_from_graph.csv")
    p.add_argument("--chunk", type=int, default=30, help="objectIdFilters values per query")
    p.add_argument("--probe", action="store_true",
                   help="Submit a single-file query to validate objectIdFilters matching, then exit")
    return p.parse_args()


async def load_saved_files(pool: asyncpg.Pool, run_ids: list[str]) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.event_id, t.file_name, t.review_status,
                   t.full_ssn_found, t.full_dl_number_found,
                   t.full_dl_image_found, t.full_financial_found,
                   e.object_id, e.web_url
            FROM targeted_pii_review t JOIN events e ON e.event_id = t.event_id
            WHERE t.run_id = ANY($1::text[]) AND t.saved_path <> ''
            """,
            run_ids,
        )
    return [dict(r) for r in rows]


async def create_query(auth: GraphAuth, start: datetime, end: datetime,
                       object_ids: list[str]) -> str:
    body = {
        "displayName": f"legal access pull {end.date()}",
        "filterStartDateTime": start.isoformat(),
        "filterEndDateTime": end.isoformat(),
        "operationFilters": FILE_ACCESS_OPS,
        "recordTypeFilters": RECORD_TYPES,
        "objectIdFilters": object_ids,
    }
    headers = {"Authorization": f"Bearer {auth.get_access_token()}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{GRAPH_BETA}/security/auditLog/queries", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    logger.info("Query %s created (status=%s, %d objectIdFilters)", data["id"], data.get("status"), len(object_ids))
    return data["id"]


async def wait_for_query(auth: GraphAuth, query_id: str) -> None:
    url = f"{GRAPH_BETA}/security/auditLog/queries/{query_id}"
    elapsed = 0
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        while elapsed < QUERY_TIMEOUT_S:
            headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 502, 503, 504):
                    await asyncio.sleep(POLL_INTERVAL_S)
                    elapsed += POLL_INTERVAL_S
                    continue
                raise
            status = resp.json().get("status", "unknown")
            if status == "succeeded":
                return
            if status == "failed":
                raise RuntimeError(f"Audit query {query_id} failed: {resp.json()}")
            await asyncio.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S
    raise TimeoutError(f"Audit query {query_id} did not complete within {QUERY_TIMEOUT_S}s")


async def fetch_records(auth: GraphAuth, query_id: str) -> list[dict]:
    url = f"{GRAPH_BETA}/security/auditLog/queries/{query_id}/records"
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        while url:
            headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
    return out


def chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def derive_row(record: dict, meta: dict, upn_domain: str) -> dict:
    raw = record.get("auditData", "{}")
    audit = json.loads(raw) if isinstance(raw, str) else (raw or {})
    upn = record.get("userPrincipalName") or audit.get("UserId", "") or record.get("userId", "")
    user_type = audit.get("UserType", "")
    # UserType 0=Regular,2=Admin,3=DcAdmin,4=System,5=Application,6=ServicePrincipal,7=CustomPolicy...
    upn_l = (upn or "").lower()
    is_internal = bool(upn_domain) and upn_domain.lower() in upn_l and "#ext#" not in upn_l
    if "urn:spo:anon" in upn_l:
        is_internal = False
    row = dict(meta)
    row.update({
        "operation": record.get("operation", ""),
        "created_utc": record.get("createdDateTime", ""),
        "user_principal_name": upn,
        "user_id": audit.get("UserId", "") or record.get("userId", ""),
        "user_type": user_type,
        "is_internal_guess": is_internal,
        "client_ip": audit.get("ClientIP", "") or audit.get("ClientIPAddress", ""),
        "workload": audit.get("Workload", ""),
        "site_url": audit.get("SiteUrl", ""),
        "object_id_audit": record.get("objectId", ""),
    })
    return row


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        raise SystemExit("ERROR: DATABASE_URL not set.")

    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=2)
    saved = await load_saved_files(pool, args.run_id)
    await pool.close()
    logger.info("Saved files to trace: %d", len(saved))

    # Match map: canonical object_id -> meta. Filter values use the DECODED url
    # because audit-log objectId stores literal (non-percent-encoded) URLs.
    by_canon: dict[str, dict] = {}
    filter_values: set[str] = set()
    for r in saved:
        meta = {
            "saved_file_name": r["file_name"], "saved_event_id": r["event_id"],
            "found_ssn": r["full_ssn_found"], "found_dl_number": r["full_dl_number_found"],
            "found_dl_image": r["full_dl_image_found"], "found_financial": r["full_financial_found"],
            "review_status": r["review_status"],
        }
        if r["object_id"]:
            by_canon[canonicalize_url(r["object_id"])] = meta
            filter_values.add(unquote(r["object_id"]))
            filter_values.add(r["object_id"])
        if r["web_url"]:
            by_canon.setdefault(canonicalize_url(r["web_url"]), meta)
            filter_values.add(r["web_url"])

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    if args.probe:
        one = next(r for r in saved if r["object_id"])
        logger.info("PROBE: %s", one["file_name"])
        qid = await create_query(auth, start, end, [unquote(one["object_id"]), one["object_id"]])
        await wait_for_query(auth, qid)
        recs = await fetch_records(auth, qid)
        print(f"\n[probe] {one['file_name']}: {len(recs)} records in last {args.days}d")
        for rec in recs[:5]:
            ad = rec.get("auditData", "{}")
            ad = json.loads(ad) if isinstance(ad, str) else ad
            print(f"  {rec.get('createdDateTime')} {rec.get('operation')} "
                  f"{rec.get('userPrincipalName') or ad.get('UserId','')}")
        return

    values = sorted(filter_values)
    logger.info("Submitting %d audit queries (chunks of %d) over %s..%s",
                (len(values) + args.chunk - 1) // args.chunk, args.chunk, start.date(), end.date())

    # Submit all chunk queries up front so they run server-side in parallel.
    query_ids = [await create_query(auth, start, end, chunk) for chunk in chunked(values, args.chunk)]

    all_records: list[dict] = []
    for qid in query_ids:
        await wait_for_query(auth, qid)
        recs = await fetch_records(auth, qid)
        logger.info("Query %s returned %d records", qid, len(recs))
        all_records.extend(recs)

    # Match records back to our files and write CSV. Dedup identical audit ids.
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen_ids: set[str] = set()
    per_file: dict[str, int] = {}
    written = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLS)
        writer.writeheader()
        for rec in all_records:
            rid = rec.get("id") or ""
            if rid and rid in seen_ids:
                continue
            seen_ids.add(rid)
            obj = (rec.get("objectId") or "").strip()
            meta = by_canon.get(canonicalize_url(obj)) if obj else None
            if meta is None:
                continue
            writer.writerow(derive_row(rec, meta, config.upn_domain))
            written += 1
            per_file[meta["saved_event_id"]] = per_file.get(meta["saved_event_id"], 0) + 1

    no_access = [r for r in saved if per_file.get(r["event_id"], 0) == 0]
    print("\n" + "=" * 64)
    print("  Graph access-event export for saved targeted-PII files")
    print("=" * 64)
    print(f"  Window:                  last {args.days} days ({start.date()}..{end.date()})")
    print(f"  Saved files:             {len(saved):>8,}")
    print(f"  Files with access rows:  {len(per_file):>8,}")
    print(f"  Files with NO access:    {len(no_access):>8,}")
    print(f"  Audit records returned:  {len(all_records):>8,}")
    print(f"  Access events written:   {written:>8,}")
    print(f"  Output CSV:              {out_path}")
    print("=" * 64)
    print("  NOTE: only events within the tenant's audit retention are returned;")
    print("  older access (e.g. 2023-2024) has aged out and is absent here.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
