"""Verdicts API endpoints."""

import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

import asyncpg
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_role

logger = logging.getLogger(__name__)

router = APIRouter(tags=["verdicts"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


class AnalystReview(BaseModel):
    disposition: Literal[
        "true_positive", "moderate_risk", "acceptable_risk",
        "needs_investigation", "false_positive",
    ]
    notes: str = ""


@router.get("/verdicts")
async def list_verdicts(
    request: Request,
    tier: Optional[str] = None,
    category: Optional[str] = None,
    provider: Optional[str] = None,
    reviewed: Optional[bool] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    pool = _pool(request)
    conditions = []
    params: list = []
    idx = 1

    if tier is not None:
        if tier == "escalated":
            conditions.append("v.escalation_tier IN ('tier_1', 'tier_2')")
        else:
            conditions.append(f"v.escalation_tier = ${idx}")
            params.append(tier)
            idx += 1
    if category is not None:
        conditions.append(f"v.category_assessments @> ${idx}::jsonb")
        params.append(json.dumps([{"id": category}]))
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


class BulkReviewRequest(BaseModel):
    event_ids: list[str] = Field(..., min_length=1, max_length=100)
    disposition: Literal[
        "true_positive", "moderate_risk", "acceptable_risk",
        "needs_investigation", "false_positive",
    ]
    notes: str = ""


@router.post("/verdicts/bulk-review")
async def bulk_review(request: Request, body: BulkReviewRequest, user=require_role("analyst")):
    pool = _pool(request)
    event_ids = list(dict.fromkeys(body.event_ids))

    user_obj = getattr(request.state, "user", None)
    reviewed_by = user_obj["email"] if user_obj else "analyst"
    now = datetime.now(timezone.utc)

    results = []
    succeeded = 0
    skipped = 0
    failed = 0
    remediation_count = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT event_id, analyst_reviewed, analyst_disposition "
                "FROM verdicts WHERE event_id = ANY($1)",
                event_ids,
            )
            verdict_map = {r["event_id"]: r for r in rows}

            existing_remediations: set[str] = set()
            if body.disposition == "true_positive":
                rem_rows = await conn.fetch(
                    "SELECT event_id FROM remediations "
                    "WHERE event_id = ANY($1) AND status IN ('pending', 'in_progress')",
                    event_ids,
                )
                existing_remediations = {r["event_id"] for r in rem_rows}

            for eid in event_ids:
                verdict = verdict_map.get(eid)
                if not verdict:
                    results.append({"event_id": eid, "status": "failed", "reason": "no_verdict"})
                    failed += 1
                    continue

                if verdict["analyst_reviewed"] and verdict["analyst_disposition"] == body.disposition:
                    results.append({"event_id": eid, "status": "skipped", "reason": "same_disposition"})
                    skipped += 1
                    continue

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
                    now, reviewed_by, body.disposition, body.notes, eid,
                )

                result_entry: dict = {"event_id": eid, "status": "succeeded"}

                if body.disposition == "true_positive" and eid not in existing_remediations:
                    rem_row = await conn.fetchrow(
                        "INSERT INTO remediations (event_id, requested_by) "
                        "VALUES ($1, $2) RETURNING id",
                        eid, reviewed_by,
                    )
                    if rem_row:
                        result_entry["remediation_id"] = rem_row["id"]
                        remediation_count += 1

                results.append(result_entry)
                succeeded += 1

    # Queue user notifications for moderate_risk (best-effort, after commit)
    if body.disposition == "moderate_risk" and succeeded > 0:
        try:
            redis_conn = request.app.state.redis
            for r in results:
                if r["status"] == "succeeded":
                    await redis_conn.rpush(
                        "sharesentinel:user_notifications",
                        json.dumps({"event_id": r["event_id"], "disposition": "moderate_risk"}),
                    )
            logger.info("Queued %d user notifications for bulk moderate_risk review", succeeded)
        except Exception:
            logger.error("Failed to queue user notifications for bulk review", exc_info=True)

    return {
        "total": len(event_ids),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "remediation_count": remediation_count,
        "results": results,
    }


@router.patch("/verdicts/{event_id}")
async def review_verdict(request: Request, event_id: str, body: AnalystReview, user=require_role("analyst")):
    pool = _pool(request)

    # Use the authenticated session identity; fall back to "analyst" when auth
    # is disabled (AUTH_DISABLED=true).
    user = getattr(request.state, "user", None)
    reviewed_by = user["email"] if user else "analyst"

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
            reviewed_by,
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
                reviewed_by,
            )
            if row:
                remediation_id = row["id"]
                remediation_status = row["status"]

    # Queue user notification for moderate_risk disposition
    if body.disposition == "moderate_risk":
        try:
            redis_conn = request.app.state.redis
            await redis_conn.rpush(
                "sharesentinel:user_notifications",
                json.dumps({"event_id": event_id, "disposition": "moderate_risk"}),
            )
            logger.info("Queued user notification for event %s (moderate_risk)", event_id)
        except Exception:
            logger.error(
                "Failed to queue user notification for event %s", event_id,
                exc_info=True,
            )

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
