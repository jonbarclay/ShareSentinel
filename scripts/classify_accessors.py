"""Add accessor_class + group-membership columns to the access-event CSVs.

For the legal review of who accessed the saved targeted-PII files, this:

  1. Classifies each access event's accessor:
       internal_uvu | guest_external | external_other | anonymous |
       tenant_anonymous | application_service | unknown
  2. Looks up, per real-user accessor, transitive membership (Graph
     checkMemberGroups) in:
       - UVU_ACTIVE_EMPLOYEE  (one security group)
       - "1"                  (ALL groups literally named "1" — there are
                               several; we report which specific one matched)

New columns appended to each CSV (rewritten in place):
    accessor_class, group_lookup_status,
    in_uvu_active_employee, in_group_1, matched_group_1

Runs inside the worker container (Graph auth available):
    docker exec sharesentinel-worker python -m scripts.classify_accessors \
        --csv /app/review_downloads/access_events_from_graph.csv \
              /app/review_downloads/access_events_for_saved_files.csv
"""

import argparse
import asyncio
import csv
import logging
import os
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("classify_accessors")

import httpx

from app.config import Config
from app.graph_api.auth import GraphAuth

GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = httpx.Timeout(60.0)
UPN_DOMAIN = "uvu.edu"

# Column-name variants across the two CSV shapes.
UPN_FIELDS = ("user_principal_name", "AccessedByUPN")
TYPE_FIELDS = ("user_type", "UserType")
NEW_COLS = ["accessor_class", "group_lookup_status",
            "in_uvu_active_employee", "in_group_1", "matched_group_1"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Classify accessors + group membership in access CSVs")
    p.add_argument("--csv", nargs="+", required=True, help="Access-event CSV path(s) to enrich")
    return p.parse_args()


def get_field(row: dict, names: tuple) -> str:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return ""


def classify(upn: str, user_type: str) -> str:
    u = (upn or "").lower()
    t = (user_type or "").strip()
    if "urn:spo:tenantanon" in u:
        return "tenant_anonymous"
    if "urn:spo:anon" in u:
        return "anonymous"
    if u.startswith("app@sharepoint") or t in ("5", "6"):
        return "application_service"
    if "#ext#" in u:
        return "guest_external"
    if u.endswith("@" + UPN_DOMAIN):
        return "internal_uvu"
    if "@" in u and "." in u:
        return "external_other"
    return "unknown"


def is_real_user(accessor_class: str) -> bool:
    return accessor_class in ("internal_uvu", "guest_external", "external_other")


async def resolve_groups(client: httpx.AsyncClient, headers: dict) -> tuple[str, dict]:
    """Return (uvu_active_employee_id, {group_1_id: displayName}) by display name."""
    async def by_name(name: str) -> list[dict]:
        r = await client.get(f"{GRAPH}/groups", headers=headers, params={
            "$filter": f"displayName eq '{name}'", "$select": "id,displayName"})
        r.raise_for_status()
        return r.json().get("value", [])

    uvu = await by_name("UVU_ACTIVE_EMPLOYEE")
    uvu_id = uvu[0]["id"] if uvu else ""
    ones = {g["id"]: g.get("displayName", "1") for g in await by_name("1")}
    logger.info("UVU_ACTIVE_EMPLOYEE id=%s | groups named '1': %d", uvu_id, len(ones))
    return uvu_id, ones


async def member_groups(client: httpx.AsyncClient, headers: dict, upn: str,
                        group_ids: list[str]) -> set[str] | None:
    """Transitive membership: which of group_ids is `upn` a member of? None on lookup failure."""
    url = f"{GRAPH}/users/{quote(upn)}/checkMemberGroups"
    try:
        r = await client.post(url, headers=headers, json={"groupIds": group_ids})
        if r.status_code == 404:
            return None  # user not found (deleted/guest not in directory)
        r.raise_for_status()
        return set(r.json().get("value", []))
    except httpx.HTTPStatusError as e:
        logger.warning("checkMemberGroups failed for %s: %s", upn, e.response.status_code)
        return None


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    auth = GraphAuth(
        tenant_id=config.azure_tenant_id, client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )
    headers = {"Authorization": f"Bearer {auth.get_access_token()}", "Content-Type": "application/json"}

    # 1. Collect distinct real-user accessor UPNs across all CSVs.
    distinct: set[str] = set()
    tables: list[tuple[str, list[str], list[dict]]] = []
    for path in args.csv:
        if not os.path.exists(path):
            logger.warning("CSV not found, skipping: %s", path)
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        tables.append((path, fieldnames, rows))
        for row in rows:
            cls = classify(get_field(row, UPN_FIELDS), get_field(row, TYPE_FIELDS))
            if is_real_user(cls):
                distinct.add(get_field(row, UPN_FIELDS).strip())
    logger.info("Distinct real-user accessors to look up: %d", len(distinct))

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        uvu_id, ones = await resolve_groups(client, headers)
        all_ids = ([uvu_id] if uvu_id else []) + list(ones.keys())

        # 2. Per-accessor transitive membership (cached).
        membership: dict[str, set | None] = {}
        for i, upn in enumerate(sorted(distinct), 1):
            # refresh token periodically for long lists
            if i % 50 == 0:
                headers["Authorization"] = f"Bearer {auth.get_access_token()}"
            membership[upn.lower()] = await member_groups(client, headers, upn, all_ids)
            await asyncio.sleep(0.05)

    # 3. Rewrite each CSV with the new columns.
    matched_one_ids: dict[str, int] = {}
    summary = {"internal_uvu": 0, "guest_external": 0, "external_other": 0,
               "anonymous": 0, "tenant_anonymous": 0, "application_service": 0, "unknown": 0}
    for path, fieldnames, rows in tables:
        out_fields = fieldnames + [c for c in NEW_COLS if c not in fieldnames]
        for row in rows:
            upn = get_field(row, UPN_FIELDS).strip()
            cls = classify(upn, get_field(row, TYPE_FIELDS))
            summary[cls] = summary.get(cls, 0) + 1
            row["accessor_class"] = cls
            if not is_real_user(cls):
                row["group_lookup_status"] = "not_a_user"
                row["in_uvu_active_employee"] = ""
                row["in_group_1"] = ""
                row["matched_group_1"] = ""
                continue
            matched = membership.get(upn.lower())
            if matched is None:
                row["group_lookup_status"] = "user_not_found"
                row["in_uvu_active_employee"] = ""
                row["in_group_1"] = ""
                row["matched_group_1"] = ""
                continue
            row["group_lookup_status"] = "checked"
            row["in_uvu_active_employee"] = bool(uvu_id and uvu_id in matched)
            one_hits = [gid for gid in ones if gid in matched]
            for gid in one_hits:
                matched_one_ids[gid] = matched_one_ids.get(gid, 0) + 1
            row["in_group_1"] = bool(one_hits)
            row["matched_group_1"] = ";".join(one_hits)

        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=out_fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in out_fields})
        os.replace(tmp, path)
        logger.info("Rewrote %s (%d rows, +%d cols)", path, len(rows), len(NEW_COLS))

    print("\n" + "=" * 60)
    print("  Accessor classification across all access events")
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k:<22} {v:>6,}")
    print("-" * 60)
    print("  Which group(s) named '1' actually matched accessors:")
    if matched_one_ids:
        for gid, n in sorted(matched_one_ids.items(), key=lambda x: -x[1]):
            print(f"    {gid}  ({ones.get(gid,'?')})  -> {n} events")
    else:
        print("    (none of the '1' groups matched any accessor)")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
