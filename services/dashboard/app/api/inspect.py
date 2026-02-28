"""Inspection queue API endpoints.

Provides endpoints to list pending inspection items and trigger processing
of Loop, OneNote, and Whiteboard content using delegated Graph API tokens.
"""

import logging
import os
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import config
from ..auth import get_current_user
from ..auth_graph import get_graph_token
from ..inspect.processor import process_inspection_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inspect", tags=["inspect"])


class ProcessRequest(BaseModel):
    event_ids: Optional[list[str]] = None


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


async def _require_auth(request: Request) -> dict:
    """Check authentication, handling both auth-enabled and auth-disabled modes."""
    if not config.AUTH_ENABLED:
        return {"email": "anonymous", "name": "Anonymous"}
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _build_ai_config() -> dict:
    """Build AI configuration from environment variables."""
    provider = os.environ.get("AI_PROVIDER", "anthropic")
    api_key = os.environ.get(f"{provider.upper()}_API_KEY", "")
    model_defaults = {
        "anthropic": "claude-sonnet-4-5-20250929",
        "openai": "gpt-4o",
        "gemini": "gemini-2.0-flash",
    }
    model = os.environ.get(
        f"{provider.upper()}_MODEL",
        model_defaults.get(provider, "claude-sonnet-4-5-20250929"),
    )
    max_tokens = int(os.environ.get("AI_MAX_TOKENS", "1024"))
    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "max_tokens": max_tokens,
    }


@router.get("/pending")
async def list_pending(request: Request):
    """List events with status 'pending_manual_inspection'.

    Returns counts by content_type, total count, items, and graph token status.
    """
    user = await _require_auth(request)

    pool = _pool(request)
    token = await get_graph_token(request)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.event_id, e.file_name, e.content_type,
                   e.user_id, e.site_url, e.drive_id, e.item_id,
                   e.received_at, e.sharing_type,
                   up.display_name AS user_display_name
            FROM events e
            LEFT JOIN user_profiles up ON e.user_id = up.user_id
            WHERE e.status = 'pending_manual_inspection'
            ORDER BY e.received_at ASC
            """
        )
        count_rows = await conn.fetch(
            """
            SELECT content_type, COUNT(*) AS cnt
            FROM events
            WHERE status = 'pending_manual_inspection'
            GROUP BY content_type
            """
        )

    counts = {r["content_type"]: r["cnt"] for r in count_rows}
    total = sum(counts.values())

    return {
        "counts": counts,
        "total": total,
        "items": [dict(r) for r in rows],
        "has_graph_token": token is not None,
    }


@router.post("/process")
async def process_batch(request: Request, body: Optional[ProcessRequest] = None):
    """Batch process pending inspection items.

    Optionally accepts a list of event_ids to process. If omitted,
    processes all pending_manual_inspection items.
    """
    user = await _require_auth(request)

    token = await get_graph_token(request)
    if not token:
        raise HTTPException(
            status_code=403,
            detail="No Graph API token available. Please log in with SSO to grant delegated permissions.",
        )

    pool = _pool(request)
    ai_config = _build_ai_config()

    if not ai_config["api_key"]:
        raise HTTPException(
            status_code=500,
            detail=f"AI provider '{ai_config['provider']}' API key not configured.",
        )

    # Fetch items to process
    event_ids = body.event_ids if body and body.event_ids else None
    async with pool.acquire() as conn:
        if event_ids:
            rows = await conn.fetch(
                """
                SELECT * FROM events
                WHERE event_id = ANY($1) AND status = 'pending_manual_inspection'
                ORDER BY received_at ASC
                """,
                event_ids,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM events
                WHERE status = 'pending_manual_inspection'
                ORDER BY received_at ASC
                """
            )

    if not rows:
        return {"processed": 0, "completed": 0, "failed": 0, "results": []}

    results = []
    completed = 0
    failed = 0

    for row in rows:
        event = dict(row)
        result = await process_inspection_item(event, token, pool, ai_config)
        results.append({
            "event_id": event["event_id"],
            "file_name": event.get("file_name"),
            **result,
        })
        if result.get("status") == "completed":
            completed += 1
        else:
            failed += 1

    return {
        "processed": len(results),
        "completed": completed,
        "failed": failed,
        "results": results,
    }


@router.post("/{event_id}")
async def process_single(request: Request, event_id: str):
    """Process a single inspection item by event_id."""
    user = await _require_auth(request)

    token = await get_graph_token(request)
    if not token:
        raise HTTPException(
            status_code=403,
            detail="No Graph API token available. Please log in with SSO to grant delegated permissions.",
        )

    pool = _pool(request)
    ai_config = _build_ai_config()

    if not ai_config["api_key"]:
        raise HTTPException(
            status_code=500,
            detail=f"AI provider '{ai_config['provider']}' API key not configured.",
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM events WHERE event_id = $1 AND status = 'pending_manual_inspection'",
            event_id,
        )

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Event {event_id} not found or not in pending_manual_inspection status.",
        )

    event = dict(row)
    result = await process_inspection_item(event, token, pool, ai_config)

    return {
        "event_id": event_id,
        "file_name": event.get("file_name"),
        **result,
    }
