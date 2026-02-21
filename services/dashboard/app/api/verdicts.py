"""Verdicts API endpoints."""

from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

router = APIRouter(tags=["verdicts"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


class AnalystReview(BaseModel):
    disposition: str
    notes: str = ""
    reviewed_by: str = ""


@router.get("/verdicts")
async def list_verdicts(
    request: Request,
    rating: Optional[int] = None,
    provider: Optional[str] = None,
    reviewed: Optional[bool] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if rating is not None:
        conditions.append(f"v.sensitivity_rating = ${idx}")
        params.append(rating)
        idx += 1
    if provider:
        conditions.append(f"v.ai_provider = ${idx}")
        params.append(provider)
        idx += 1
    if reviewed is not None:
        conditions.append(f"v.analyst_reviewed = ${idx}")
        params.append(reviewed)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM verdicts v {where}", *params
        )
        rows = await conn.fetch(
            f"""
            SELECT v.*, e.file_name, e.user_id, e.object_id
            FROM verdicts v
            JOIN events e ON v.event_id = e.event_id
            {where}
            ORDER BY v.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, per_page, offset,
        )

    return {
        "total": count_row["total"],
        "page": page,
        "per_page": per_page,
        "verdicts": [dict(r) for r in rows],
    }


@router.patch("/verdicts/{event_id}")
async def review_verdict(request: Request, event_id: str, body: AnalystReview):
    pool = _pool(request)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE verdicts
            SET analyst_reviewed = TRUE,
                analyst_reviewed_at = $1,
                analyst_reviewed_by = $2,
                analyst_disposition = $3,
                analyst_notes = $4
            WHERE event_id = $5
            """,
            datetime.now(timezone.utc),
            body.reviewed_by,
            body.disposition,
            body.notes,
            event_id,
        )
    # Auto-create remediation request on true_positive
    remediation_id = None
    remediation_status = None
    if body.disposition == "true_positive":
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO remediations (event_id, requested_by)
                VALUES ($1, $2)
                RETURNING id, status
                """,
                event_id,
                body.reviewed_by or "analyst",
            )
            if row:
                remediation_id = row["id"]
                remediation_status = row["status"]

    return {
        "status": "updated",
        "event_id": event_id,
        "remediation_id": remediation_id,
        "remediation_status": remediation_status,
    }


@router.get("/remediations/{event_id}")
async def get_remediation(request: Request, event_id: str):
    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, event_id, requested_by, requested_at, action_type,
                   status, started_at, completed_at,
                   permissions_removed, permissions_failed,
                   report_sent, error_message
            FROM remediations
            WHERE event_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            event_id,
        )
    if not row:
        return {"remediation": None}
    return {"remediation": dict(row)}
