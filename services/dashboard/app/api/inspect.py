"""Inspection queue API endpoints.

Provides endpoints to list pending inspection items and trigger processing
of Loop, OneNote, and Whiteboard content using browser screenshots of
sharing URLs with saved auth state.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config
from ..auth import get_current_user
from ..inspect.processor import process_inspection_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inspect", tags=["inspect"])


class ProcessRequest(BaseModel):
    event_ids: Optional[list[str]] = None
    process_all: bool = False


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

    Returns counts by content_type, total count, items, and browser auth status.
    """
    user = await _require_auth(request)

    pool = _pool(request)
    redis = request.app.state.redis

    # Check browser auth state
    from ..inspect import browser_auth
    auth_status = await browser_auth.get_auth_state_status(redis)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.event_id, e.file_name, e.content_type,
                   e.user_id, e.site_url, e.drive_id, e.item_id_graph,
                   e.received_at, e.sharing_type,
                   up.display_name AS user_display_name
            FROM events e
            LEFT JOIN user_profiles up ON e.user_id = up.user_id
            WHERE e.status = 'pending_manual_inspection'
            ORDER BY e.received_at DESC
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
        "has_browser_auth": auth_status.get("authenticated", False),
    }


BATCH_LOCK_KEY = "sharesentinel:inspect:batch_lock"
BATCH_LOCK_TTL = 7200  # 2 hours (browser screenshots are slow, process-all can be large)
BATCH_STATUS_KEY_PREFIX = "sharesentinel:inspect:batch:"
BATCH_STATUS_TTL = 3600  # keep results for 1 hour


async def _run_batch_background(
    batch_id: str,
    rows: list,
    pool,
    ai_config: dict,
    redis,
) -> None:
    """Process inspection items in the background, updating Redis with progress."""
    total = len(rows)
    completed = 0
    failed = 0
    results = []
    status_key = f"{BATCH_STATUS_KEY_PREFIX}{batch_id}"

    try:
        for i, row in enumerate(rows):
            event = dict(row)

            # Update progress
            progress = {
                "status": "processing",
                "total": total,
                "current": i + 1,
                "completed": completed,
                "failed": failed,
                "results": results,
            }
            await redis.set(status_key, json.dumps(progress), ex=BATCH_STATUS_TTL)

            result = await process_inspection_item(event, pool, ai_config)
            item_result = {
                "event_id": event["event_id"],
                "file_name": event.get("file_name"),
                **result,
            }
            results.append(item_result)
            if result.get("status") == "completed":
                completed += 1
            else:
                failed += 1

        # Final status
        final = {
            "status": "done",
            "total": total,
            "current": total,
            "completed": completed,
            "failed": failed,
            "results": results,
        }
        await redis.set(status_key, json.dumps(final), ex=BATCH_STATUS_TTL)

    except Exception as exc:
        logger.exception("Background batch %s failed", batch_id)
        error_status = {
            "status": "error",
            "total": total,
            "current": len(results),
            "completed": completed,
            "failed": failed,
            "results": results,
            "error": str(exc),
        }
        await redis.set(status_key, json.dumps(error_status), ex=BATCH_STATUS_TTL)
    finally:
        await redis.delete(BATCH_LOCK_KEY)


@router.post("/process")
async def process_batch(request: Request, body: Optional[ProcessRequest] = None):
    """Start async batch processing of pending inspection items.

    Returns 202 with a batch_id. Poll /process/status/{batch_id} for progress.
    """
    user = await _require_auth(request)

    redis = request.app.state.redis
    if not await redis.set(BATCH_LOCK_KEY, "1", nx=True, ex=BATCH_LOCK_TTL):
        raise HTTPException(409, "Another batch is already running.")

    pool = _pool(request)
    ai_config = _build_ai_config()

    if not ai_config["api_key"]:
        await redis.delete(BATCH_LOCK_KEY)
        raise HTTPException(
            status_code=500,
            detail=f"AI provider '{ai_config['provider']}' API key not configured.",
        )

    # Atomically claim items
    event_ids = body.event_ids if body and body.event_ids else None
    process_all = body.process_all if body else False
    async with pool.acquire() as conn:
        async with conn.transaction():
            if event_ids:
                rows = await conn.fetch(
                    """
                    UPDATE events SET status = 'inspecting'
                    WHERE event_id IN (
                        SELECT event_id FROM events
                        WHERE event_id = ANY($1) AND status = 'pending_manual_inspection'
                        ORDER BY received_at DESC
                        FOR UPDATE SKIP LOCKED
                    ) RETURNING *
                    """,
                    event_ids,
                )
            elif process_all:
                rows = await conn.fetch(
                    """
                    UPDATE events SET status = 'inspecting'
                    WHERE event_id IN (
                        SELECT event_id FROM events
                        WHERE status = 'pending_manual_inspection'
                        ORDER BY received_at DESC
                        FOR UPDATE SKIP LOCKED
                    ) RETURNING *
                    """
                )
            else:
                rows = await conn.fetch(
                    """
                    UPDATE events SET status = 'inspecting'
                    WHERE event_id IN (
                        SELECT event_id FROM events
                        WHERE status = 'pending_manual_inspection'
                        ORDER BY received_at DESC
                        LIMIT 10
                        FOR UPDATE SKIP LOCKED
                    ) RETURNING *
                    """
                )

    if not rows:
        await redis.delete(BATCH_LOCK_KEY)
        return {"batch_id": None, "processed": 0, "message": "No items to process"}

    batch_id = str(uuid.uuid4())

    # Store initial status
    initial = {
        "status": "processing",
        "total": len(rows),
        "current": 0,
        "completed": 0,
        "failed": 0,
        "results": [],
    }
    await redis.set(f"{BATCH_STATUS_KEY_PREFIX}{batch_id}", json.dumps(initial), ex=BATCH_STATUS_TTL)

    # Launch background task
    asyncio.create_task(
        _run_batch_background(batch_id, rows, pool, ai_config, redis)
    )

    return JSONResponse(
        status_code=202,
        content={"batch_id": batch_id, "total": len(rows), "message": "Processing started"},
    )


@router.get("/process/status/{batch_id}")
async def process_status(request: Request, batch_id: str):
    """Poll for batch processing progress."""
    await _require_auth(request)
    redis = request.app.state.redis
    raw = await redis.get(f"{BATCH_STATUS_KEY_PREFIX}{batch_id}")
    if raw is None:
        raise HTTPException(404, "Batch not found or expired")
    return json.loads(raw)


@router.post("/{event_id}")
async def process_single(request: Request, event_id: str):
    """Process a single inspection item by event_id."""
    user = await _require_auth(request)

    pool = _pool(request)
    ai_config = _build_ai_config()

    if not ai_config["api_key"]:
        raise HTTPException(
            status_code=500,
            detail=f"AI provider '{ai_config['provider']}' API key not configured.",
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE events SET status = 'inspecting'
                WHERE event_id = (
                    SELECT event_id FROM events
                    WHERE event_id = $1 AND status = 'pending_manual_inspection'
                    FOR UPDATE SKIP LOCKED
                ) RETURNING *
                """,
                event_id,
            )

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Event {event_id} not found or not in pending_manual_inspection status.",
        )

    event = dict(row)
    result = await process_inspection_item(event, pool, ai_config)

    return {
        "event_id": event_id,
        "file_name": event.get("file_name"),
        **result,
    }


# --- Browser auth session endpoints ---


@router.post("/browser-session/start")
async def browser_session_start(request: Request):
    """Start an interactive browser auth session for org-wide screenshots."""
    await _require_auth(request)
    from ..inspect import browser_auth
    try:
        await browser_auth.start_session(config.SHAREPOINT_ROOT_URL)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "started", "viewport": {"width": 1920, "height": 1080}}


@router.post("/browser-session/close")
async def browser_session_close(request: Request):
    """Close the browser auth session and save cookies if authenticated."""
    await _require_auth(request)
    from ..inspect import browser_auth
    redis = request.app.state.redis
    saved = await browser_auth.close_session(redis)
    return {"status": "closed", "auth_saved": saved}


@router.get("/browser-session/status")
async def browser_session_status(request: Request):
    """Check if a valid browser auth state exists in Redis."""
    await _require_auth(request)
    from ..inspect import browser_auth
    redis = request.app.state.redis
    return await browser_auth.get_auth_state_status(redis)


@router.websocket("/browser-session/stream")
async def browser_session_stream(websocket: WebSocket):
    """WebSocket: stream browser viewport as JPEG frames for interactive auth."""
    # Manual cookie auth — middleware doesn't intercept WS upgrades
    from ..auth import _unsign_session_id
    cookie_header = websocket.headers.get("cookie", "")
    session_id = None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ss_session="):
            signed = part[len("ss_session="):]
            session_id = _unsign_session_id(signed)
            break

    if config.AUTH_ENABLED:
        if not session_id:
            await websocket.close(code=4001, reason="Not authenticated")
            return
        session_redis = websocket.app.state.session_redis
        raw = await session_redis.get(f"ss:session:{session_id}")
        if raw is None:
            await websocket.close(code=4001, reason="Session expired")
            return

    from ..inspect import browser_auth
    redis = websocket.app.state.redis
    await browser_auth.handle_browser_stream(websocket, redis)
