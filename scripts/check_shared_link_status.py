"""Verify sharing-link status for unreviewed Tier 1 / Tier 2 events and
auto-disposition the ones whose links are already gone.

Background
----------
The "Events Needing Review" queue is every parent event whose latest verdict is
``escalation_tier IN ('tier_1','tier_2')`` and ``analyst_reviewed`` is not TRUE
(see ``services/dashboard/app/api/stats.py`` -> ``unreviewed_escalated``). After a
tenant-wide sweep removed all anonymous/org-wide sharing links except for a few
allow-listed users/sites, most of those events no longer have a live sharing
link, so there is nothing left to act on.

This script checks each such event's sharing link **live via the Graph API**
(``GET /drives/{driveId}/items/{itemId}/permissions``). If no *active*
anonymous/organization-scoped link remains (removed, expired, or the item itself
is gone), the event's verdict is marked ``analyst_disposition='acceptable_risk'``
with ``analyst_reviewed=TRUE`` so it drops out of the review queue. Events whose
link is still live are left untouched for a human to handle.

Removed vs. disabled — IMPORTANT
--------------------------------
This check detects links that were **removed** (the permission object is gone)
or **expired**. It does NOT, on its own, detect links that were **disabled at the
site level** — i.e. sharing turned off via a SharingCapability downgrade
(anonymous) or ``DisableCompanyWideSharingLinks`` (organization/company-wide).
Disabling a link that way leaves the permission object in place, so Graph still
lists it and this script reports the event as ``still_active`` even though the
link no longer works when clicked.

That error is one-directional and safe: site-disabled links are *kept* for
review, never wrongly closed. Any ``still_active`` result should therefore be
verified against the site's live policy before closing. The script prints the
link scope + site for every ``still_active`` item so you can check it directly,
e.g. from the lifecycle-cron container (which has PnP):

    Connect-PnPOnline -Url $env:SHAREPOINT_ADMIN_URL -ClientId $env:AZURE_CLIENT_ID \
        -Tenant $env:AZURE_TENANT_ID -CertificatePath <cert> -CertificatePassword <pw>
    Get-PnPTenantSite -Identity <site-url> |
        Select-Object Url, SharingCapability, DisableCompanyWideSharingLinks
    # anonymous link is dead unless SharingCapability = ExternalUserAndGuestSharing
    # organization link is dead when   DisableCompanyWideSharingLinks = Disabled

The check is read-only against Graph; the only writes are to the ``verdicts``
table (and optionally ``sharing_link_lifecycle`` with ``--sync-lifecycle``).
Nothing is written unless ``--commit`` is passed.

Usage (inside the worker container):
    # dry run — classify everything, write a report CSV, change nothing
    docker exec sharesentinel-worker python -m scripts.check_shared_link_status \
        --out /app/link_status_sweep.csv

    # commit the acceptable_risk dispositions
    docker exec sharesentinel-worker python -m scripts.check_shared_link_status \
        --commit --out /app/link_status_sweep.csv

Then copy the report out:
    docker cp sharesentinel-worker:/app/link_status_sweep.csv .
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import httpx

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.client import GRAPH_BASE, DEFAULT_TIMEOUT
from app.graph_api.sharing import extract_all_sharing_links, has_active_sharing_links

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("check_shared_link_status")

TIERS = ("tier_1", "tier_2")

# Outcomes that mean "no live link -> safe to mark acceptable_risk"
_MARKABLE = {"removed", "item_gone"}

# HTTP retry policy for Graph throttling / transient errors
_MAX_ATTEMPTS = 4
_BASE_BACKOFF_S = 2.0


def _active_links(links: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the anon/org links that are unexpired (mirrors has_active_sharing_links)."""
    now = datetime.now(timezone.utc)
    active: List[Dict[str, Any]] = []
    for link in links:
        exp = link.get("expiration_date")
        if exp is None:
            active.append(link)
            continue
        try:
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if exp_dt > now:
                active.append(link)
        except (ValueError, TypeError):
            active.append(link)  # unparseable -> treat as active (fail-open)
    return active


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-disposition unreviewed Tier 1/2 events whose sharing links are gone",
    )
    p.add_argument(
        "--commit", action="store_true",
        help="Write acceptable_risk dispositions. Without this the script only reports (dry run).",
    )
    p.add_argument(
        "--sync-lifecycle", action="store_true",
        help="Also mark the item's still-'active'/'ms_managed' sharing_link_lifecycle rows as "
             "manually_removed when no live link is found (prevents spurious countdown emails). "
             "Only takes effect with --commit.",
    )
    p.add_argument(
        "--tier", choices=["tier_1", "tier_2", "both"], default="both",
        help="Which tier(s) to process (default: both).",
    )
    p.add_argument(
        "--reviewed-by", default="link-status-sweep",
        help="Value stored in verdicts.analyst_reviewed_by (default: link-status-sweep).",
    )
    p.add_argument(
        "--concurrency", type=int, default=8,
        help="Max concurrent Graph permission lookups (default: 8).",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Process at most N events (0 = all). Useful for a small trial run.",
    )
    p.add_argument(
        "--out", default="/app/link_status_sweep.csv",
        help="Report CSV path inside the container (default: /app/link_status_sweep.csv).",
    )
    return p.parse_args()


async def _list_permissions(
    client: httpx.AsyncClient,
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
) -> Tuple[str, List[Dict[str, Any]], str]:
    """List permissions for a drive item.

    Returns ``(state, permissions, detail)`` where state is one of:
      * ``"ok"``          — permissions fetched (list may be empty)
      * ``"item_gone"``   — item/drive returned 404 (nothing left to share)
      * ``"access_denied"``— 403 (cannot determine; leave for human)
      * ``"error"``       — other/persistent failure after retries
    """
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/permissions"

    last_detail = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
            resp = await client.get(url, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_detail = f"transport error: {exc}"
            await asyncio.sleep(_BASE_BACKOFF_S * attempt)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return "error", [], "invalid permissions JSON"
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                return "error", [], "missing permissions list"
            if data.get("@odata.nextLink"):
                return "error", [], "incomplete permissions list (additional pages); keep for review"
            if any(not isinstance(p, dict) for p in data["value"]):
                return "error", [], "invalid permission entry"
            return "ok", data["value"], ""
        if resp.status_code == 404:
            return "item_gone", [], "item/drive not found (404)"
        if resp.status_code == 403:
            return "access_denied", [], "access denied (403)"
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else _BASE_BACKOFF_S * attempt
            except ValueError:
                delay = _BASE_BACKOFF_S * attempt
            last_detail = f"HTTP {resp.status_code} (attempt {attempt}/{_MAX_ATTEMPTS})"
            logger.warning("Throttled/transient on %s/%s: %s; sleeping %.1fs",
                           drive_id, item_id, last_detail, delay)
            await asyncio.sleep(delay)
            continue
        # Other 4xx: not retryable
        return "error", [], f"HTTP {resp.status_code}: {resp.text[:200]}"

    return "error", [], last_detail or "exhausted retries"


async def _classify_event(
    client: httpx.AsyncClient,
    auth: GraphAuth,
    sem: asyncio.Semaphore,
    event: Dict[str, Any],
) -> Dict[str, Any]:
    """Determine the sharing-link status for a single event."""
    drive_id = event.get("drive_id")
    item_id = event.get("item_id_graph")

    result: Dict[str, Any] = {
        "event_id": event["event_id"],
        "tier": event["escalation_tier"],
        "file_name": event.get("file_name") or "",
        "site_url": event.get("site_url") or "",
        "drive_id": drive_id or "",
        "item_id_graph": item_id or "",
        "active_link_count": "",
        "active_scopes": "",
        "outcome": "",
        "detail": "",
    }

    if not drive_id or not item_id:
        # No Graph handle for this item — cannot verify live. Common for
        # site-level / delegated-content events. Leave for human review.
        result["outcome"] = "no_item_ref"
        result["detail"] = "missing drive_id/item_id_graph — cannot verify via Graph"
        return result

    async with sem:
        state, perms, detail = await _list_permissions(client, auth, drive_id, item_id)

    if state == "ok":
        # Extraction requires a URL; a redacted link must not become evidence
        # that sharing has ended.
        if any("link" in p and (
            not isinstance(p["link"], dict)
            or not p["link"].get("scope")
            or not p["link"].get("webUrl")
        ) for p in perms):
            result["outcome"] = "error"
            result["detail"] = "incomplete sharing-link metadata; keep for review"
            return result
        links = extract_all_sharing_links(perms)
        if has_active_sharing_links(links):
            active = _active_links(links)
            scopes = sorted({(l.get("scope") or "").lower() for l in active})
            result["outcome"] = "still_active"
            result["active_link_count"] = len(active)
            result["active_scopes"] = ",".join(s for s in scopes if s)
            result["detail"] = (
                "active " + "/".join(scopes) + " link still present in Graph "
                "(verify it is not site-level disabled before closing)"
            )
        else:
            result["outcome"] = "removed"
            result["active_link_count"] = 0
            result["detail"] = (
                "no active anonymous/org-wide link remains"
                + (" (links present but expired)" if links else "")
            )
    else:
        # item_gone / access_denied / error
        result["outcome"] = state
        result["detail"] = detail

    return result


async def _mark_acceptable(
    pool: asyncpg.Pool,
    event_id: str,
    reviewed_by: str,
    note: str,
    now: datetime,
    sync_lifecycle: bool,
) -> Tuple[int, int]:
    """Mark an event's unreviewed verdicts as acceptable_risk.

    Returns ``(verdict_rows_updated, lifecycle_rows_updated)``.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            v_status = await conn.execute(
                """
                UPDATE verdicts
                SET analyst_reviewed = TRUE,
                    analyst_reviewed_at = $1,
                    analyst_reviewed_by = $2,
                    analyst_disposition = 'acceptable_risk',
                    analyst_notes = $3
                WHERE event_id = $4
                  AND NOT COALESCE(analyst_reviewed, FALSE)
                """,
                now, reviewed_by, note, event_id,
            )
            v_updated = int(v_status.split()[-1]) if v_status.startswith("UPDATE") else 0

            lc_updated = 0
            if sync_lifecycle:
                lc_status = await conn.execute(
                    """
                    UPDATE sharing_link_lifecycle
                    SET status = 'manually_removed',
                        removal_attempted_at = $1,
                        removal_succeeded = TRUE,
                        removal_error = NULL,
                        updated_at = $1
                    WHERE event_id = $2
                      AND status IN ('active', 'ms_managed')
                    """,
                    now, event_id,
                )
                lc_updated = int(lc_status.split()[-1]) if lc_status.startswith("UPDATE") else 0

    return v_updated, lc_updated


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set.")
        sys.exit(1)

    tiers = list(TIERS) if args.tier == "both" else [args.tier]

    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=5)

    # ── 1. Fetch the review queue (latest verdict per parent event) ──────
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (e.event_id)
                   e.event_id, e.drive_id, e.item_id_graph, e.file_name,
                   e.site_url, e.item_type, v.escalation_tier
            FROM verdicts v
            JOIN events e ON e.event_id = v.event_id
            WHERE e.parent_event_id IS NULL
              AND NOT COALESCE(v.analyst_reviewed, FALSE)
              AND v.escalation_tier = ANY($1::text[])
            ORDER BY e.event_id, v.created_at DESC
            """,
            tiers,
        )
    events = [dict(r) for r in rows]
    if args.limit and args.limit > 0:
        events = events[: args.limit]

    logger.info(
        "Fetched %d unreviewed %s event(s) to check%s",
        len(events), "/".join(tiers),
        f" (limited to {args.limit})" if args.limit else "",
    )
    if not events:
        print("Nothing to do — no unreviewed Tier 1/2 events matched.")
        await pool.close()
        return

    # ── 2. Classify each event's live sharing-link status (concurrent) ───
    sem = asyncio.Semaphore(max(1, args.concurrency))
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        results = await asyncio.gather(
            *(_classify_event(client, auth, sem, ev) for ev in events)
        )

    # ── 3. Apply dispositions for events whose links are gone ────────────
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    marked = 0
    verdict_rows = 0
    lifecycle_rows = 0

    for res in results:
        if res["outcome"] not in _MARKABLE:
            res["marked"] = "no"
            continue

        if res["outcome"] == "item_gone":
            reason = "shared item no longer exists (Graph returned 404)"
        else:
            reason = res["detail"]
        note = (
            f"Auto-dispositioned by check_shared_link_status.py on {today}: {reason}. "
            f"No live anonymous/org-wide sharing link — no action required."
        )

        if args.commit:
            v_updated, lc_updated = await _mark_acceptable(
                pool, res["event_id"], args.reviewed_by, note, now, args.sync_lifecycle,
            )
            verdict_rows += v_updated
            lifecycle_rows += lc_updated
            res["marked"] = "yes" if v_updated else "already_reviewed"
        else:
            res["marked"] = "would"
        marked += 1

    await pool.close()

    # ── 4. Write report CSV ──────────────────────────────────────────────
    fieldnames = [
        "event_id", "tier", "outcome", "marked", "active_link_count",
        "active_scopes", "file_name", "site_url", "drive_id", "item_id_graph",
        "detail",
    ]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    # ── 5. Summary ───────────────────────────────────────────────────────
    def count(outcome: str) -> int:
        return sum(1 for r in results if r["outcome"] == outcome)

    print()
    print("=" * 62)
    print("  Sharing-Link Status Sweep  " + ("[COMMIT]" if args.commit else "[DRY RUN]"))
    print("=" * 62)
    print(f"  Events checked:                {len(results):>6,}")
    print(f"    removed (no live link):      {count('removed'):>6,}")
    print(f"    item_gone (404):             {count('item_gone'):>6,}")
    print(f"    still_active (kept):         {count('still_active'):>6,}")
    print(f"    no_item_ref (can't verify):  {count('no_item_ref'):>6,}")
    print(f"    access_denied (403):         {count('access_denied'):>6,}")
    print(f"    error:                       {count('error'):>6,}")
    print("-" * 62)
    if args.commit:
        print(f"  Marked acceptable_risk:        {marked:>6,} event(s)")
        print(f"    verdict rows updated:        {verdict_rows:>6,}")
        if args.sync_lifecycle:
            print(f"    lifecycle rows updated:      {lifecycle_rows:>6,}")
    else:
        print(f"  Would mark acceptable_risk:    {marked:>6,} event(s)  (re-run with --commit)")
    print(f"  Report: {args.out}")
    print("=" * 62)

    still = [r for r in results if r["outcome"] == "still_active"]
    if still:
        print()
        print(f"  {len(still)} event(s) kept as still_active — VERIFY against site policy")
        print("  (a link disabled at the site level still shows here; see the module")
        print("   docstring for the Get-PnPTenantSite recipe):")
        for r in still:
            print(f"    - [{r['tier']}] scope={r['active_scopes'] or '?'}  {r['file_name']!r}")
            print(f"        site={r['site_url'] or '(unknown — derive from link)'}  event_id={r['event_id']}")


if __name__ == "__main__":
    asyncio.run(main())
