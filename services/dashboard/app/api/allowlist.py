"""Allow list API endpoints for managing SharePoint site anonymous sharing."""

import json
import logging
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["allowlist"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


# --- Request models ---

class AddSiteRequest(BaseModel):
    site_id: str
    site_url: str
    site_display_name: str = ""
    added_by: str = ""
    notes: str = ""


class TriggerSyncRequest(BaseModel):
    triggered_by: str = ""


# --- Allow list CRUD ---

@router.get("/allowlist/sites")
async def list_allowlist_sites(
    request: Request,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if q:
        conditions.append(
            f"(site_display_name ILIKE ${idx} OR site_url ILIKE ${idx})"
        )
        params.append(f"%{q}%")
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
async def add_allowlist_site(request: Request, body: AddSiteRequest):
    pool = _pool(request)
    user = getattr(request.state, "user", None)
    added_by = user["email"] if user else (body.added_by or "unknown")

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
    return {"site": dict(row)}


@router.delete("/allowlist/sites/{site_db_id}")
async def remove_allowlist_site(request: Request, site_db_id: int):
    pool = _pool(request)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM site_allowlist WHERE id = $1", site_db_id
        )
    deleted = result == "DELETE 1"
    return {"deleted": deleted, "id": site_db_id}


# --- Site search via Graph API ---

@router.get("/allowlist/sites/search")
async def search_sharepoint_sites(
    request: Request,
    q: str = Query("", min_length=1),
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
async def trigger_sync(request: Request, body: TriggerSyncRequest):
    pool = _pool(request)
    user = getattr(request.state, "user", None)
    triggered_by = user["email"] if user else (body.triggered_by or "unknown")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO site_allowlist_syncs (trigger_type, triggered_by, status)
            VALUES ('manual', $1, 'pending')
            RETURNING id
            """,
            triggered_by,
        )
    sync_id = row["id"]

    # Push trigger to Redis for lifecycle-cron to pick up
    redis_conn = request.app.state.redis
    await redis_conn.rpush(
        "sharesentinel:allowlist_sync_trigger",
        json.dumps({"sync_id": sync_id}),
    )
    logger.info("Manual allowlist sync triggered (sync_id=%d)", sync_id)

    return {"sync_id": sync_id}


@router.get("/allowlist/syncs")
async def list_syncs(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
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
async def get_sync_detail(request: Request, sync_id: int):
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
