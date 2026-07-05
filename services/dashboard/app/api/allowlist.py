"""Allow list API endpoints for managing SharePoint site anonymous sharing."""

import json
import logging
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_role
from ..config import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["allowlist"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


def _escape_ilike(value: str) -> str:
    """Escape SQL ILIKE wildcard characters in user input."""
    return value.replace("%", "\\%").replace("_", "\\_")


# --- Request models ---

class AddSiteRequest(BaseModel):
    site_id: str = Field(..., min_length=1, max_length=500)
    site_url: str = Field(..., min_length=1, max_length=2000)
    site_display_name: str = Field("", max_length=500)
    added_by: str = Field("", max_length=255)
    notes: str = Field("", max_length=2000)


class TriggerSyncRequest(BaseModel):
    triggered_by: str = Field("", max_length=255)


class AddVisibilitySiteRequest(BaseModel):
    group_id: str = Field(..., min_length=1, max_length=500)
    site_url: str = Field("", max_length=2000)
    group_display_name: str = Field("", max_length=500)
    notes: str = Field("", max_length=2000)


class TriggerScanRequest(BaseModel):
    triggered_by: str = Field("", max_length=255)


class AddCwsAllowlistRequest(BaseModel):
    site_id: str = Field(..., min_length=1, max_length=500)
    site_url: str = Field(..., min_length=1, max_length=2000)
    site_display_name: str = Field("", max_length=500)
    notes: str = Field("", max_length=2000)


class DiagnoseFailureRequest(BaseModel):
    site_url: str = Field(..., min_length=1, max_length=2000)


# --- Allow list CRUD ---

@router.get("/allowlist/sites")
async def list_allowlist_sites(
    request: Request,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if q:
        conditions.append(
            f"(site_display_name ILIKE ${idx} OR site_url ILIKE ${idx})"
        )
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM site_allowlist {where}", *params
        )
        rows = await conn.fetch(
            f"""
            SELECT id, site_id, site_url, site_display_name, added_by, notes,
                   created_at, updated_at
            FROM site_allowlist
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "sites": [dict(r) for r in rows],
    }


@router.post("/allowlist/sites")
@limiter.limit("20/minute")
async def add_allowlist_site(request: Request, body: AddSiteRequest, user=require_role("admin")):
    pool = _pool(request)
    added_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_allowlist (site_id, site_url, site_display_name, added_by, notes)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (site_id) DO UPDATE SET
                site_url = EXCLUDED.site_url,
                site_display_name = EXCLUDED.site_display_name,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            RETURNING id, site_id, site_url, site_display_name, added_by, notes, created_at
            """,
            body.site_id,
            body.site_url,
            body.site_display_name,
            added_by,
            body.notes,
        )

    # Trigger immediate sharing enablement via lifecycle-cron
    try:
        redis_conn = request.app.state.redis
        await redis_conn.rpush(
            "sharesentinel:site_policy_action",
            json.dumps({
                "action": "enable_sharing",
                "site_url": body.site_url,
                "site_display_name": body.site_display_name,
                "triggered_by": added_by,
            }),
        )
        logger.info("Queued enable_sharing action for %s", body.site_url)
    except Exception as e:
        logger.error("Failed to queue enable_sharing action: %s", e)

    return {"site": dict(row)}


@router.delete("/allowlist/sites/{site_db_id}")
@limiter.limit("20/minute")
async def remove_allowlist_site(request: Request, site_db_id: int, user=require_role("admin")):
    pool = _pool(request)
    removed_by = user.get("email", "unknown")

    # Fetch site info before deleting so we can trigger the revoke action
    async with pool.acquire() as conn:
        site_row = await conn.fetchrow(
            "SELECT site_url, site_display_name FROM site_allowlist WHERE id = $1",
            site_db_id,
        )
        result = await conn.execute(
            "DELETE FROM site_allowlist WHERE id = $1", site_db_id
        )
    deleted = result == "DELETE 1"

    # Trigger immediate sharing disable via lifecycle-cron
    if deleted and site_row and site_row["site_url"]:
        try:
            redis_conn = request.app.state.redis
            await redis_conn.rpush(
                "sharesentinel:site_policy_action",
                json.dumps({
                    "action": "disable_sharing",
                    "site_url": site_row["site_url"],
                    "site_display_name": site_row["site_display_name"] or "",
                    "triggered_by": removed_by,
                }),
            )
            logger.info("Queued disable_sharing action for %s", site_row["site_url"])
        except Exception as e:
            logger.error("Failed to queue disable_sharing action: %s", e)

    return {"deleted": deleted, "id": site_db_id}


# --- Site details via Graph API ---

@router.get("/allowlist/site-details")
@limiter.limit("30/minute")
async def get_site_details_endpoint(
    request: Request,
    site_url: str = Query(""),
    group_id: str = Query(""),
    _user=require_role("analyst"),
):
    """Fetch live site/group details from Graph API (visibility, sharing, owners, members)."""
    from ..graph_helper import get_site_details, is_configured

    if not is_configured():
        return {"details": None, "error": "Graph API not configured"}

    if not site_url and not group_id:
        return {"details": None, "error": "Provide site_url or group_id"}

    try:
        details = await get_site_details(site_url=site_url, group_id=group_id)
    except Exception as e:
        logger.error("Site details fetch failed: %s", e, exc_info=True)
        return {"details": None, "error": "Failed to fetch site details"}

    # Enrich with sharing capability from the most recent policy event if available
    pool = _pool(request)
    if site_url:
        async with pool.acquire() as conn:
            cap_row = await conn.fetchrow(
                """
                SELECT new_value FROM site_policy_events
                WHERE site_url ILIKE $1 AND policy_type = 'sharing'
                ORDER BY created_at DESC LIMIT 1
                """,
                site_url.rstrip("/").lower(),
            )
            if cap_row:
                details["sharing_capability"] = cap_row["new_value"]
            else:
                # Check if this site was scanned but had no violation (already correct)
                # Try to get from the last full scan's evaluation
                cap_row = await conn.fetchrow(
                    """
                    SELECT new_value, previous_value FROM site_policy_events
                    WHERE site_url ILIKE $1
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    f"%{_escape_ilike(site_url.rstrip('/').split('/sites/')[-1])}%",
                )
                if cap_row:
                    details["sharing_capability"] = cap_row["new_value"]

    return {"details": details}


# --- Site search via Graph API ---

@router.get("/allowlist/sites/search")
@limiter.limit("30/minute")
async def search_sharepoint_sites(
    request: Request,
    q: str = Query("", min_length=1),
    _user=require_role("admin"),
):
    from ..graph_helper import search_sites, is_configured

    if not is_configured():
        return {"sites": [], "error": "Graph API not configured"}

    try:
        graph_sites = await search_sites(q, top=20)
    except Exception as e:
        logger.error("Graph API site search failed: %s", e, exc_info=True)
        return {"sites": [], "error": "Site search failed. Check server logs for details."}

    # Cross-reference with existing allow list
    pool = _pool(request)
    async with pool.acquire() as conn:
        allowed_rows = await conn.fetch("SELECT site_id FROM site_allowlist")
    allowed_ids = {r["site_id"] for r in allowed_rows}

    results = []
    for site in graph_sites:
        results.append({
            "site_id": site["id"],
            "display_name": site["displayName"],
            "web_url": site["webUrl"],
            "already_allowed": site["id"] in allowed_ids,
        })

    return {"sites": results}


# --- Sync trigger + history ---

@router.post("/allowlist/sync")
@limiter.limit("5/minute")
async def trigger_sync(request: Request, body: TriggerSyncRequest, user=require_role("admin")):
    """Legacy alias for the full site policy scan.

    The original allowlist sync queue (``sharesentinel:allowlist_sync_trigger``)
    lost its consumer when enforcement moved into the site policy scanner, so
    rows it created stayed 'pending' forever. The site policy scan is a
    superset of the old sync (sharing capability + visibility), so this
    endpoint now triggers that instead.
    """
    pool = _pool(request)
    triggered_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_policy_scans (trigger_type, triggered_by, status)
            VALUES ('manual', $1, 'pending')
            RETURNING id
            """,
            triggered_by,
        )
    scan_id = row["id"]

    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:site_policy_trigger",
        json.dumps({"scan_id": scan_id}),
    )
    logger.info(
        "Manual allowlist sync triggered -> site policy scan (scan_id=%d)", scan_id
    )

    return {"sync_id": scan_id, "scan_id": scan_id}


@router.get("/allowlist/syncs")
async def list_syncs(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM site_allowlist_syncs"
        )
        rows = await conn.fetch(
            """
            SELECT id, trigger_type, triggered_by, status,
                   started_at, completed_at,
                   total_sites_checked, sites_disabled, sites_enabled,
                   sites_already_correct, sites_failed, error_message,
                   created_at
            FROM site_allowlist_syncs
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "syncs": [dict(r) for r in rows],
    }


@router.get("/allowlist/syncs/{sync_id}")
async def get_sync_detail(request: Request, sync_id: int, _user=require_role("analyst")):
    pool = _pool(request)
    async with pool.acquire() as conn:
        sync_row = await conn.fetchrow(
            """
            SELECT id, trigger_type, triggered_by, status,
                   started_at, completed_at,
                   total_sites_checked, sites_disabled, sites_enabled,
                   sites_already_correct, sites_failed, error_message,
                   created_at
            FROM site_allowlist_syncs
            WHERE id = $1
            """,
            sync_id,
        )
        detail_rows = await conn.fetch(
            """
            SELECT id, site_id, site_url, site_display_name,
                   previous_capability, desired_capability,
                   action_taken, error_message, created_at
            FROM site_allowlist_sync_details
            WHERE sync_id = $1
            ORDER BY created_at
            """,
            sync_id,
        )

    if not sync_row:
        return {"sync": None, "details": []}

    return {
        "sync": dict(sync_row),
        "details": [dict(r) for r in detail_rows],
    }


# --- Visibility Allow List CRUD ---

@router.get("/visibility-allowlist/sites")
async def list_visibility_allowlist(
    request: Request,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if q:
        conditions.append(
            f"(group_display_name ILIKE ${idx} OR site_url ILIKE ${idx} OR group_id ILIKE ${idx})"
        )
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM site_visibility_allowlist {where}", *params
        )
        rows = await conn.fetch(
            f"""
            SELECT id, group_id, site_url, group_display_name, added_by, notes,
                   created_at, updated_at
            FROM site_visibility_allowlist
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "sites": [dict(r) for r in rows],
    }


@router.post("/visibility-allowlist/sites")
@limiter.limit("20/minute")
async def add_visibility_site(
    request: Request,
    body: AddVisibilitySiteRequest,
    user=require_role("admin"),
):
    pool = _pool(request)
    added_by = user.get("email", "unknown")

    # Graph returns lowercase group GUIDs; the scanner/provisioning compares
    # case-insensitively but store the normalized form so ON CONFLICT dedups.
    body.group_id = (body.group_id or "").strip().lower()

    # Resolve site URL from group_id if not provided
    site_url = body.site_url
    if not site_url and body.group_id:
        try:
            from ..graph_helper import _get_access_token, _resolve_group_site_url
            token = _get_access_token()
            site_url = await _resolve_group_site_url(token, body.group_id)
        except Exception as e:
            logger.warning("Failed to resolve site URL for group %s: %s", body.group_id, e)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_visibility_allowlist
                (group_id, site_url, group_display_name, added_by, notes)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (group_id) DO UPDATE SET
                site_url = EXCLUDED.site_url,
                group_display_name = EXCLUDED.group_display_name,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            RETURNING id, group_id, site_url, group_display_name, added_by, notes, created_at
            """,
            body.group_id,
            site_url,
            body.group_display_name,
            added_by,
            body.notes,
        )

    # Trigger immediate visibility change via lifecycle-cron
    try:
        redis_conn = request.app.state.redis
        await redis_conn.rpush(
            "sharesentinel:site_policy_action",
            json.dumps({
                "action": "set_public",
                "group_id": body.group_id,
                "group_display_name": body.group_display_name,
                "site_url": site_url,
                "triggered_by": added_by,
            }),
        )
        logger.info("Queued set_public action for group %s", body.group_id)
    except Exception as e:
        logger.error("Failed to queue set_public action: %s", e)

    return {"site": dict(row)}


@router.delete("/visibility-allowlist/sites/{site_db_id}")
@limiter.limit("20/minute")
async def remove_visibility_site(
    request: Request,
    site_db_id: int,
    user=require_role("admin"),
):
    pool = _pool(request)
    removed_by = user.get("email", "unknown")

    # Fetch group info before deleting so we can trigger the revoke action
    async with pool.acquire() as conn:
        group_row = await conn.fetchrow(
            "SELECT group_id, site_url, group_display_name FROM site_visibility_allowlist WHERE id = $1",
            site_db_id,
        )
        result = await conn.execute(
            "DELETE FROM site_visibility_allowlist WHERE id = $1", site_db_id
        )
    deleted = result == "DELETE 1"

    # Trigger immediate set-to-Private via lifecycle-cron
    if deleted and group_row and group_row["group_id"]:
        try:
            redis_conn = request.app.state.redis
            await redis_conn.rpush(
                "sharesentinel:site_policy_action",
                json.dumps({
                    "action": "set_private",
                    "group_id": group_row["group_id"],
                    "group_display_name": group_row["group_display_name"] or "",
                    "site_url": group_row["site_url"] or "",
                    "triggered_by": removed_by,
                }),
            )
            logger.info("Queued set_private action for group %s", group_row["group_id"])
        except Exception as e:
            logger.error("Failed to queue set_private action: %s", e)

    return {"deleted": deleted, "id": site_db_id}


@router.get("/visibility-allowlist/sites/search")
@limiter.limit("30/minute")
async def search_m365_groups_endpoint(
    request: Request,
    q: str = Query("", min_length=1),
    _user=require_role("admin"),
):
    from ..graph_helper import search_m365_groups, is_configured

    if not is_configured():
        return {"groups": [], "error": "Graph API not configured"}

    try:
        graph_groups = await search_m365_groups(q, top=20)
    except Exception as e:
        logger.error("Graph API group search failed: %s", e, exc_info=True)
        return {"groups": [], "error": "Group search failed. Check server logs."}

    # Cross-reference with existing visibility allow list
    pool = _pool(request)
    async with pool.acquire() as conn:
        allowed_rows = await conn.fetch("SELECT group_id FROM site_visibility_allowlist")
    allowed_ids = {r["group_id"] for r in allowed_rows}

    results = []
    for g in graph_groups:
        results.append({
            "group_id": g["id"],
            "display_name": g["displayName"],
            "visibility": g.get("visibility", ""),
            "site_url": g.get("siteUrl", ""),
            "already_allowed": g["id"] in allowed_ids,
        })

    return {"groups": results}


# --- Site Policy Events + Scans ---

@router.get("/site-policy/events")
async def list_policy_events(
    request: Request,
    policy_type: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if policy_type:
        conditions.append(f"policy_type = ${idx}")
        params.append(policy_type)
        idx += 1

    if q:
        conditions.append(
            f"(site_display_name ILIKE ${idx} OR site_url ILIKE ${idx})"
        )
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM site_policy_events {where}", *params
        )
        rows = await conn.fetch(
            f"""
            SELECT id, scan_id, policy_type, site_url, site_display_name,
                   group_id, previous_value, new_value, action, error_message,
                   created_at
            FROM site_policy_events
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "events": [dict(r) for r in rows],
    }


@router.get("/site-policy/events/summary")
async def policy_events_summary(request: Request, _user=require_role("analyst")):
    pool = _pool(request)
    async with pool.acquire() as conn:
        last_scan = await conn.fetchrow(
            """
            SELECT id, completed_at, total_sites_scanned,
                   visibility_violations_found, visibility_remediated,
                   sharing_violations_found, sharing_remediated, errors
            FROM site_policy_scans
            WHERE status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
            """
        )
        counts = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE policy_type = 'visibility' AND action = 'remediated') AS visibility_remediated_30d,
                COUNT(*) FILTER (WHERE policy_type = 'sharing' AND action = 'remediated') AS sharing_remediated_30d,
                COUNT(*) FILTER (WHERE action = 'failed') AS errors_30d
            FROM site_policy_events
            WHERE created_at > NOW() - INTERVAL '30 days'
            """
        )

    return {
        "last_scan": dict(last_scan) if last_scan else None,
        "last_30_days": dict(counts) if counts else {
            "visibility_remediated_30d": 0,
            "sharing_remediated_30d": 0,
            "errors_30d": 0,
        },
    }


@router.post("/site-policy/scan")
@limiter.limit("5/minute")
async def trigger_policy_scan(
    request: Request,
    body: TriggerScanRequest,
    user=require_role("admin"),
):
    pool = _pool(request)
    triggered_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_policy_scans (trigger_type, triggered_by, status)
            VALUES ('manual', $1, 'pending')
            RETURNING id
            """,
            triggered_by,
        )
    scan_id = row["id"]

    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:site_policy_trigger",
        json.dumps({"scan_id": scan_id}),
    )
    logger.info("Manual site policy scan triggered (scan_id=%d)", scan_id)

    return {"scan_id": scan_id}


@router.get("/site-policy/scans")
async def list_policy_scans(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM site_policy_scans"
        )
        rows = await conn.fetch(
            """
            SELECT id, trigger_type, triggered_by, status,
                   started_at, completed_at,
                   total_sites_scanned, visibility_violations_found,
                   visibility_remediated, sharing_violations_found,
                   sharing_remediated, errors, error_message,
                   created_at
            FROM site_policy_scans
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "scans": [dict(r) for r in rows],
    }


@router.get("/site-policy/scans/{scan_id}")
async def get_policy_scan_detail(request: Request, scan_id: int, _user=require_role("analyst")):
    pool = _pool(request)
    async with pool.acquire() as conn:
        scan_row = await conn.fetchrow(
            """
            SELECT id, trigger_type, triggered_by, status,
                   started_at, completed_at,
                   total_sites_scanned, visibility_violations_found,
                   visibility_remediated, sharing_violations_found,
                   sharing_remediated, errors, error_message,
                   created_at
            FROM site_policy_scans
            WHERE id = $1
            """,
            scan_id,
        )
        event_rows = await conn.fetch(
            """
            SELECT id, policy_type, site_url, site_display_name,
                   group_id, previous_value, new_value, action,
                   error_message, created_at
            FROM site_policy_events
            WHERE scan_id = $1
            ORDER BY created_at
            """,
            scan_id,
        )

    if not scan_row:
        return {"scan": None, "events": []}

    return {
        "scan": dict(scan_row),
        "events": [dict(r) for r in event_rows],
    }


# --- Company-Wide Sharing (DisableCompanyWideSharingLinks) ---

import os as _os  # keep imports scoped to this section


def _cws_exception_group_id() -> str:
    return _os.environ.get("COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID", "").strip()


@router.get("/company-wide-sharing/allowlist")
async def list_cws_allowlist(
    request: Request,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if q:
        conditions.append(f"(site_display_name ILIKE ${idx} OR site_url ILIKE ${idx})")
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM site_company_wide_sharing_allowlist {where}",
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT id, site_id, site_url, site_display_name, added_by, notes,
                   created_at, updated_at
            FROM site_company_wide_sharing_allowlist
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "sites": [dict(r) for r in rows],
    }


@router.post("/company-wide-sharing/allowlist")
@limiter.limit("20/minute")
async def add_cws_allowlist_site(
    request: Request,
    body: AddCwsAllowlistRequest,
    user=require_role("admin"),
):
    pool = _pool(request)
    added_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_company_wide_sharing_allowlist
                (site_id, site_url, site_display_name, added_by, notes)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (site_id) DO UPDATE SET
                site_url = EXCLUDED.site_url,
                site_display_name = EXCLUDED.site_display_name,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            RETURNING id, site_id, site_url, site_display_name, added_by, notes, created_at
            """,
            body.site_id,
            body.site_url,
            body.site_display_name,
            added_by,
            body.notes,
        )

    try:
        redis_conn = request.app.state.redis
        await redis_conn.rpush(
            "sharesentinel:company_wide_sharing_action",
            json.dumps({
                "action": "cws_allow_site",
                "site_url": body.site_url,
                "site_display_name": body.site_display_name,
                "triggered_by": added_by,
            }),
        )
        logger.info("Queued cws_allow_site action for %s", body.site_url)
    except Exception as e:
        logger.error("Failed to queue cws_allow_site action: %s", e)

    return {"site": dict(row)}


@router.delete("/company-wide-sharing/allowlist/{site_db_id}")
@limiter.limit("20/minute")
async def remove_cws_allowlist_site(
    request: Request,
    site_db_id: int,
    user=require_role("admin"),
):
    pool = _pool(request)
    removed_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        site_row = await conn.fetchrow(
            "SELECT site_url, site_display_name FROM site_company_wide_sharing_allowlist WHERE id = $1",
            site_db_id,
        )
        result = await conn.execute(
            "DELETE FROM site_company_wide_sharing_allowlist WHERE id = $1",
            site_db_id,
        )
    deleted = result == "DELETE 1"

    if deleted and site_row and site_row["site_url"]:
        try:
            redis_conn = request.app.state.redis
            await redis_conn.rpush(
                "sharesentinel:company_wide_sharing_action",
                json.dumps({
                    "action": "cws_disallow_site",
                    "site_url": site_row["site_url"],
                    "site_display_name": site_row["site_display_name"] or "",
                    "triggered_by": removed_by,
                }),
            )
            logger.info("Queued cws_disallow_site action for %s", site_row["site_url"])
        except Exception as e:
            logger.error("Failed to queue cws_disallow_site action: %s", e)

    return {"deleted": deleted, "id": site_db_id}


@router.get("/company-wide-sharing/allowlist/search")
@limiter.limit("30/minute")
async def search_cws_sites(request: Request, q: str = Query("", min_length=1), _user=require_role("admin")):
    from ..graph_helper import is_configured, search_sites

    if not is_configured():
        return {"sites": [], "error": "Graph API not configured"}

    try:
        graph_sites = await search_sites(q, top=20)
    except Exception as e:
        logger.error("Graph API site search failed: %s", e, exc_info=True)
        return {"sites": [], "error": "Site search failed. Check server logs."}

    pool = _pool(request)
    async with pool.acquire() as conn:
        allowed_rows = await conn.fetch(
            "SELECT site_id FROM site_company_wide_sharing_allowlist"
        )
    allowed_ids = {r["site_id"] for r in allowed_rows}

    results = [
        {
            "site_id": s["id"],
            "display_name": s["displayName"],
            "web_url": s["webUrl"],
            "already_allowed": s["id"] in allowed_ids,
        }
        for s in graph_sites
    ]
    return {"sites": results}


@router.get("/company-wide-sharing/exception-group/members")
async def list_cws_exception_group_members(request: Request, _user=require_role("analyst")):
    """Read-only list of members of the Entra ID group that exempts users' OneDrive sites."""
    from ..graph_helper import get_group_members, is_configured

    group_id = _cws_exception_group_id()
    if not group_id:
        return {
            "group_id": "",
            "members": [],
            "error": "COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID is not configured",
        }
    if not is_configured():
        return {"group_id": group_id, "members": [], "error": "Graph API not configured"}

    try:
        members = await get_group_members(group_id, top=999)
    except Exception as e:
        logger.error("Failed to load CWS exception group members: %s", e, exc_info=True)
        return {
            "group_id": group_id,
            "members": [],
            "error": "Failed to load members. Check server logs.",
        }

    return {"group_id": group_id, "members": members, "count": len(members)}


@router.post("/company-wide-sharing/scan")
@limiter.limit("5/minute")
async def trigger_cws_scan(
    request: Request,
    body: TriggerScanRequest,
    user=require_role("admin"),
):
    """Triggers an immediate drift-remediation pass against current
    ``cws_verification_events``. The CWS pipeline is PowerShell-based as of the
    Set-PnPTenantSite migration; this endpoint creates a placeholder row in
    ``company_wide_sharing_scans`` and signals ``cws_drift_remediation_loop``,
    which runs ``Invoke-CwsDriftRemediation.ps1 -ScanId <id>`` to adopt the row.
    """
    pool = _pool(request)
    triggered_by = user.get("email", "unknown")
    dry_run = _os.environ.get("CWS_DRIFT_REMEDIATION_DRY_RUN", "true").lower() == "true"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO company_wide_sharing_scans
                (trigger_type, triggered_by, status, dry_run)
            VALUES ('manual', $1, 'pending', $2)
            RETURNING id
            """,
            triggered_by, dry_run,
        )
    scan_id = row["id"]

    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:cws_drift_remediation_trigger",
        json.dumps({"scan_id": scan_id, "triggered_by": triggered_by}),
    )
    logger.info("Manual CWS drift remediation triggered (scan_id=%d, dry_run=%s)", scan_id, dry_run)
    return {"scan_id": scan_id, "dry_run": dry_run}


@router.get("/company-wide-sharing/scans")
async def list_cws_scans(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    offset = (page - 1) * per_page
    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM company_wide_sharing_scans"
        )
        rows = await conn.fetch(
            """
            SELECT id, trigger_type, triggered_by, status, dry_run,
                   started_at, completed_at,
                   total_sites_scanned, violations_found, remediated,
                   would_remediate, skipped_allowlist, skipped_exception_group,
                   already_compliant, errors, exception_group_member_count,
                   error_message, created_at
            FROM company_wide_sharing_scans
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "scans": [dict(r) for r in rows],
    }


@router.get("/company-wide-sharing/scans/{scan_id}")
async def get_cws_scan_detail(request: Request, scan_id: int, _user=require_role("analyst")):
    pool = _pool(request)
    async with pool.acquire() as conn:
        scan_row = await conn.fetchrow(
            """
            SELECT id, trigger_type, triggered_by, status, dry_run,
                   started_at, completed_at,
                   total_sites_scanned, violations_found, remediated,
                   would_remediate, skipped_allowlist, skipped_exception_group,
                   already_compliant, errors, exception_group_member_count,
                   error_message, created_at
            FROM company_wide_sharing_scans
            WHERE id = $1
            """,
            scan_id,
        )
        event_rows = await conn.fetch(
            """
            SELECT id, site_url, site_display_name, site_type, user_upn,
                   previous_value, new_value, action, exempt_reason,
                   error_message, created_at
            FROM company_wide_sharing_events
            WHERE scan_id = $1
            ORDER BY created_at
            """,
            scan_id,
        )

    if not scan_row:
        return {"scan": None, "events": []}

    return {"scan": dict(scan_row), "events": [dict(r) for r in event_rows]}


@router.get("/company-wide-sharing/events")
async def list_cws_events(
    request: Request,
    q: Optional[str] = None,
    action: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if action:
        conditions.append(f"action = ${idx}")
        params.append(action)
        idx += 1

    if q:
        conditions.append(
            f"(site_display_name ILIKE ${idx} OR site_url ILIKE ${idx} OR user_upn ILIKE ${idx})"
        )
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM company_wide_sharing_events {where}",
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT id, scan_id, site_url, site_display_name, site_type, user_upn,
                   previous_value, new_value, action, exempt_reason,
                   error_message, created_at
            FROM company_wide_sharing_events
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "events": [dict(r) for r in rows],
    }


@router.get("/company-wide-sharing/failures")
async def list_cws_failures(
    request: Request,
    q: Optional[str] = None,
    category: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    """List sites/OneDrives whose latest CWS event is `failed` — i.e. should be
    Disabled but the Set-PnPTenantSite call did not succeed. Each row is
    enriched with the most recent Diagnose-OneDriveSite.ps1 result when one
    has been computed.
    """
    pool = _pool(request)
    conditions: list[str] = []
    params: list = []
    idx = 1

    if category:
        conditions.append(f"d.failure_category = ${idx}")
        params.append(category)
        idx += 1

    if q:
        conditions.append(
            f"(f.site_url ILIKE ${idx} OR f.site_display_name ILIKE ${idx} OR f.user_upn ILIKE ${idx})"
        )
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS total
            FROM cws_current_failures f
            LEFT JOIN cws_failure_diagnostics d ON d.site_url = f.site_url
            {where}
            """,
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT f.event_id, f.scan_id, f.site_url, f.site_display_name,
                   f.site_type, f.user_upn, f.previous_value, f.new_value,
                   f.error_message, f.failed_at,
                   d.failure_category, d.diagnostic_summary,
                   d.last_diagnosed_at, d.diagnostic_exit_code,
                   d.source_event_id AS diagnostic_event_id
            FROM cws_current_failures f
            LEFT JOIN cws_failure_diagnostics d ON d.site_url = f.site_url
            {where}
            ORDER BY f.failed_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

        category_rows = await conn.fetch(
            """
            SELECT COALESCE(d.failure_category, 'undiagnosed') AS failure_category,
                   COUNT(*) AS count
            FROM cws_current_failures f
            LEFT JOIN cws_failure_diagnostics d ON d.site_url = f.site_url
            GROUP BY 1
            ORDER BY 2 DESC
            """
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "failures": [dict(r) for r in rows],
        "category_counts": [dict(r) for r in category_rows],
    }


@router.post("/company-wide-sharing/failures/diagnose")
@limiter.limit("30/minute")
async def trigger_cws_failure_diagnose(
    request: Request,
    body: DiagnoseFailureRequest,
    user=require_role("analyst"),
):
    """Enqueue a re-diagnostic for one currently-failing site. The lifecycle-cron
    `cws_failure_diagnostic_loop` picks up the trigger, runs
    Invoke-CwsFailureDiagnostic.ps1 (which shells out to
    Diagnose-OneDriveSite.ps1), and upserts a fresh row in
    cws_failure_diagnostics."""
    pool = _pool(request)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM cws_current_failures WHERE site_url = $1",
            body.site_url,
        )
    if not row:
        return {
            "queued": False,
            "reason": "site is not currently in cws_current_failures",
        }

    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:cws_failure_diagnostic_trigger",
        json.dumps({
            "site_url": body.site_url,
            "triggered_by": user.get("email", "unknown"),
        }),
    )
    logger.info(
        "Manual CWS failure diagnostic queued for %s (by %s)",
        body.site_url, user.get("email", "unknown"),
    )
    return {"queued": True}


@router.post("/company-wide-sharing/failures/remediate")
@limiter.limit("20/minute")
async def trigger_cws_failure_remediate(
    request: Request,
    body: DiagnoseFailureRequest,
    user=require_role("admin"),
):
    """Manually retry the disable-CWS action against a currently-failing site.

    Pushes a `cws_disallow_site` message onto the shared
    `sharesentinel:company_wide_sharing_action` queue. The lifecycle-cron
    `cws_allowlist_action_loop` picks it up, allocates a scan row, and runs
    Invoke-CwsAllowlistAction.ps1, which sets
    DisableCompanyWideSharingLinks=Disabled on the target site. A success
    here clears the row from cws_current_failures on the next event refresh.
    """
    pool = _pool(request)
    triggered_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT site_url, site_display_name
            FROM cws_current_failures
            WHERE site_url = $1
            """,
            body.site_url,
        )
    if not row:
        return {
            "queued": False,
            "reason": "site is not currently in cws_current_failures",
        }

    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:company_wide_sharing_action",
        json.dumps({
            "action": "cws_disallow_site",
            "site_url": row["site_url"],
            "site_display_name": row["site_display_name"] or "",
            "triggered_by": triggered_by,
        }),
    )
    logger.info(
        "Manual CWS remediation queued for %s (by %s)",
        row["site_url"], triggered_by,
    )
    return {"queued": True}


# ---------------------------------------------------------------------------
# CWS Verification Sweep (detect-only weekly cross-check at tenant scale)
# ---------------------------------------------------------------------------


@router.post("/cws-verification/scan")
@limiter.limit("2/minute")
async def trigger_cws_verification(
    request: Request,
    user=require_role("admin"),
):
    """Manually trigger a full verification sweep. Detect-only — no remediation."""
    pool = _pool(request)
    triggered_by = user.get("email", "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO cws_verification_scans
                (trigger_type, triggered_by, status)
            VALUES ('manual', $1, 'pending')
            RETURNING id
            """,
            triggered_by,
        )
    scan_id = row["id"]

    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:cws_verification_trigger",
        json.dumps({"scan_id": scan_id}),
    )
    logger.info("Manual CWS verification sweep triggered (scan_id=%d)", scan_id)
    return {"scan_id": scan_id}


@router.get("/cws-verification/scans")
async def list_cws_verification_scans(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    offset = (page - 1) * per_page
    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM cws_verification_scans"
        )
        rows = await conn.fetch(
            """
            SELECT id, trigger_type, triggered_by, status,
                   started_at, completed_at,
                   total_candidates, checked, drifts_found, unprovisioned_count,
                   ok_count, errors, exception_group_member_count,
                   last_checkpoint_cursor, error_message, created_at
            FROM cws_verification_scans
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "scans": [dict(r) for r in rows],
    }


@router.get("/cws-verification/scans/{scan_id}")
async def get_cws_verification_scan_detail(
    request: Request, scan_id: int, _user=require_role("analyst"),
):
    pool = _pool(request)
    async with pool.acquire() as conn:
        scan_row = await conn.fetchrow(
            """
            SELECT id, trigger_type, triggered_by, status,
                   started_at, completed_at,
                   total_candidates, checked, drifts_found, unprovisioned_count,
                   ok_count, errors, exception_group_member_count,
                   last_checkpoint_cursor, error_message, created_at
            FROM cws_verification_scans
            WHERE id = $1
            """,
            scan_id,
        )
        event_rows = await conn.fetch(
            """
            SELECT id, site_url, site_display_name, site_type, user_upn,
                   expected_value, actual_value, drift_type,
                   exempt_reason, trigger_source, error_message, created_at
            FROM cws_verification_events
            WHERE scan_id = $1
            ORDER BY created_at
            LIMIT 1000
            """,
            scan_id,
        )

    if not scan_row:
        return {"scan": None, "events": []}

    return {"scan": dict(scan_row), "events": [dict(r) for r in event_rows]}


@router.get("/cws-verification/events")
async def list_cws_verification_events(
    request: Request,
    q: Optional[str] = None,
    drift_type: Optional[str] = None,
    trigger_source: Optional[str] = None,
    scan_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _user=require_role("analyst"),
):
    pool = _pool(request)
    conditions: list[str] = []
    params: list = []
    idx = 1

    if drift_type:
        conditions.append(f"drift_type = ${idx}")
        params.append(drift_type)
        idx += 1
    if trigger_source:
        conditions.append(f"trigger_source = ${idx}")
        params.append(trigger_source)
        idx += 1
    if scan_id is not None:
        conditions.append(f"scan_id = ${idx}")
        params.append(scan_id)
        idx += 1
    if q:
        conditions.append(
            f"(site_url ILIKE ${idx} OR user_upn ILIKE ${idx})"
        )
        params.append(f"%{_escape_ilike(q)}%")
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM cws_verification_events {where}",
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT id, scan_id, site_url, site_display_name, site_type, user_upn,
                   expected_value, actual_value, drift_type,
                   exempt_reason, trigger_source, error_message, created_at
            FROM cws_verification_events
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "events": [dict(r) for r in rows],
    }
