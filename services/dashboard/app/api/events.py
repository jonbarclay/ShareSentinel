"""Events API endpoints."""

from datetime import datetime
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["events"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


@router.get("/events")
async def list_events(
    request: Request,
    status: Optional[str] = None,
    user: Optional[str] = None,
    item_type: Optional[str] = None,
    content_type: Optional[str] = None,
    file_name: Optional[str] = None,
    since: Optional[str] = None,
    tier: Optional[str] = None,
    category: Optional[str] = None,
    reviewed: Optional[bool] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if status:
        conditions.append(f"e.status = ${idx}")
        params.append(status)
        idx += 1
    if user:
        conditions.append(f"e.user_id ILIKE ${idx}")
        params.append(f"%{user}%")
        idx += 1
    if item_type:
        conditions.append(f"e.item_type = ${idx}")
        params.append(item_type)
        idx += 1
    if content_type:
        conditions.append(f"e.content_type = ${idx}")
        params.append(content_type)
        idx += 1
    if file_name:
        conditions.append(f"COALESCE(e.confirmed_file_name, e.file_name) ILIKE ${idx}")
        params.append(f"%{file_name}%")
        idx += 1
    site_url = request.query_params.get("site_url")
    if site_url:
        conditions.append(f"e.site_url ILIKE ${idx}")
        params.append(f"%{site_url}%")
        idx += 1
    if since:
        conditions.append(f"e.received_at > ${idx}")
        params.append(datetime.fromisoformat(since))
        idx += 1
    if tier is not None:
        if tier == "escalated":
            conditions.append("v.escalation_tier IN ('tier_1', 'tier_2')")
        else:
            conditions.append(f"v.escalation_tier = ${idx}")
            params.append(tier)
            idx += 1
    if category is not None:
        conditions.append(f"v.category_assessments @> ${idx}::jsonb")
        params.append([{"id": category}])
        idx += 1
    if reviewed is not None:
        conditions.append(f"COALESCE(v.analyst_reviewed, FALSE) = ${idx}")
        params.append(reviewed)
        idx += 1

    # Exclude child events from the main list (they appear under their parent)
    conditions.append("e.parent_event_id IS NULL")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"""SELECT COUNT(*) AS total FROM events e
            LEFT JOIN LATERAL (
                SELECT * FROM verdicts WHERE event_id = e.event_id ORDER BY created_at DESC LIMIT 1
            ) v ON true
            {where}""",
            *params,
        )
        total = count_row["total"]

        rows = await conn.fetch(
            f"""
            SELECT e.*, v.escalation_tier, v.category_assessments,
                   v.overall_context, v.analysis_mode,
                   v.ai_provider, v.analyst_reviewed, v.analyst_disposition,
                   v.risk_score,
                   up.display_name AS user_display_name
            FROM events e
            LEFT JOIN LATERAL (
                SELECT * FROM verdicts WHERE event_id = e.event_id ORDER BY created_at DESC LIMIT 1
            ) v ON true
            LEFT JOIN user_profiles up ON e.user_id = up.user_id
            {where}
            ORDER BY e.received_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "events": [dict(r) for r in rows],
    }


@router.get("/events/{event_id}")
async def get_event(request: Request, event_id: str):
    pool = _pool(request)
    async with pool.acquire() as conn:
        event = await conn.fetchrow("SELECT * FROM events WHERE event_id = $1", event_id)
        verdict = await conn.fetchrow(
            "SELECT * FROM verdicts WHERE event_id = $1 ORDER BY created_at DESC LIMIT 1", event_id
        )
        audit_rows = await conn.fetch(
            "SELECT * FROM audit_log WHERE event_id = $1 ORDER BY created_at", event_id
        )
        user_profile = None
        if event:
            up_row = await conn.fetchrow(
                "SELECT * FROM user_profiles WHERE user_id = $1", event["user_id"]
            )
            user_profile = dict(up_row) if up_row else None
    # Fetch child events if this is a folder (parent)
    child_events = []
    if event and event["item_type"] and event["item_type"].lower() == "folder":
        async with pool.acquire() as conn:
            child_rows = await conn.fetch(
                """
                SELECT e.event_id, e.file_name, e.relative_path, e.status,
                       e.failure_reason, e.child_index, e.file_size_bytes,
                       e.mime_type, e.web_url,
                       v.escalation_tier, v.category_assessments,
                       v.summary, v.analysis_mode
                FROM events e
                LEFT JOIN LATERAL (
                    SELECT * FROM verdicts WHERE event_id = e.event_id ORDER BY created_at DESC LIMIT 1
                ) v ON true
                WHERE e.parent_event_id = $1
                ORDER BY e.child_index
                """,
                event_id,
            )
            child_events = [dict(r) for r in child_rows]

    # Fetch sharing link lifecycle records
    lifecycle_records = []
    if event:
        async with pool.acquire() as conn:
            lc_rows = await conn.fetch(
                """
                SELECT link_created_at, ms_expiration_at, status,
                       link_created_at + INTERVAL '180 days' AS enforced_expiration_at,
                       file_name, sharing_scope, sharing_type, link_url, permission_id
                FROM sharing_link_lifecycle
                WHERE event_id = $1
                ORDER BY link_created_at
                """,
                event_id,
            )
            lifecycle_records = [dict(r) for r in lc_rows]

    # Fetch parent event info for child files (folder cascade context)
    parent_event = None
    parent_lifecycle = []
    if event and event.get("parent_event_id"):
        async with pool.acquire() as conn:
            pe_row = await conn.fetchrow(
                """
                SELECT event_id, file_name, sharing_links, status,
                       sharing_type, sharing_scope
                FROM events WHERE event_id = $1
                """,
                event["parent_event_id"],
            )
            if pe_row:
                parent_event = dict(pe_row)
            pl_rows = await conn.fetch(
                """
                SELECT link_created_at, ms_expiration_at, status,
                       link_created_at + INTERVAL '180 days' AS enforced_expiration_at,
                       file_name, sharing_scope, sharing_type, link_url, permission_id
                FROM sharing_link_lifecycle
                WHERE event_id = $1
                ORDER BY link_created_at
                """,
                event["parent_event_id"],
            )
            parent_lifecycle = [dict(r) for r in pl_rows]

    return {
        "event": dict(event) if event else None,
        "verdict": dict(verdict) if verdict else None,
        "audit_log": [dict(r) for r in audit_rows],
        "user_profile": user_profile,
        "child_events": child_events,
        "lifecycle": lifecycle_records,
        "parent_event": parent_event,
        "parent_lifecycle": parent_lifecycle,
    }
