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

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM events e LEFT JOIN verdicts v ON e.event_id = v.event_id {where}",
            *params,
        )
        total = count_row["total"]

        rows = await conn.fetch(
            f"""
            SELECT e.*, v.escalation_tier, v.category_assessments,
                   v.overall_context, v.analysis_mode,
                   v.ai_provider, v.analyst_reviewed, v.analyst_disposition,
                   up.display_name AS user_display_name
            FROM events e
            LEFT JOIN verdicts v ON e.event_id = v.event_id
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
        verdict = await conn.fetchrow("SELECT * FROM verdicts WHERE event_id = $1", event_id)
        audit_rows = await conn.fetch(
            "SELECT * FROM audit_log WHERE event_id = $1 ORDER BY created_at", event_id
        )
        user_profile = None
        if event:
            up_row = await conn.fetchrow(
                "SELECT * FROM user_profiles WHERE user_id = $1", event["user_id"]
            )
            user_profile = dict(up_row) if up_row else None
    return {
        "event": dict(event) if event else None,
        "verdict": dict(verdict) if verdict else None,
        "audit_log": [dict(r) for r in audit_rows],
        "user_profile": user_profile,
    }
