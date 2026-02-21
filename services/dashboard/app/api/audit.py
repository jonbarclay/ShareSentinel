"""Audit log API endpoint."""

from typing import Optional

import asyncpg
from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["audit"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


@router.get("/audit-log")
async def list_audit_log(
    request: Request,
    event_id: Optional[str] = None,
    action: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if event_id:
        conditions.append(f"event_id = ${idx}")
        params.append(event_id)
        idx += 1
    if action:
        conditions.append(f"action = ${idx}")
        params.append(action)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM audit_log {where}", *params
        )
        rows = await conn.fetch(
            f"""
            SELECT * FROM audit_log {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "entries": [dict(r) for r in rows],
    }
