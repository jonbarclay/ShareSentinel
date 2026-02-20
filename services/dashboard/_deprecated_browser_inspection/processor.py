"""Orchestrate inspection processing for a single event.

Handles Loop, OneNote, and Whiteboard content types by taking browser
screenshots of sharing URLs using saved auth state, then running
AI analysis on the captured images.
"""

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {"loop", "onenote", "whiteboard"}


async def process_inspection_item(
    event: dict,
    db_pool,
    ai_config: dict,
) -> dict:
    """Process a single inspection item end-to-end.

    Takes a browser screenshot of the item's sharing URL using saved
    auth state, then runs AI image analysis on the screenshot.

    Args:
        event: Database row dict with event_id, content_type, item_id_graph,
               file_name, sharing_link_url, object_id, etc.
        db_pool: asyncpg connection pool.
        ai_config: Dict with provider, api_key, model, max_tokens.

    Returns:
        Dict with status, content_type, summary, categories.
    """
    event_id = event["event_id"]
    content_type = event.get("content_type", "unknown")
    file_name = event.get("file_name", "unknown")
    item_id = event.get("item_id_graph")
    sharing_link_url = event.get("sharing_link_url")
    object_id = event.get("object_id")

    share_url = sharing_link_url or object_id

    if content_type not in SUPPORTED_TYPES:
        reason = f"Unsupported content_type: {content_type}"
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {"status": "failed", "content_type": content_type, "reason": reason}

    if not share_url:
        reason = "No sharing URL available (missing sharing_link_url and object_id)"
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {"status": "failed", "content_type": content_type, "reason": reason}

    try:
        from .browser_fetcher import take_screenshot, _load_auth_state

        auth_state = await _load_auth_state()
        if not auth_state:
            reason = "No browser auth state — authenticate via the Inspection Queue first"
            await _mark_inspection_failed(event_id, db_pool, reason)
            return {"status": "failed", "content_type": content_type, "reason": reason}

        with tempfile.TemporaryDirectory(prefix=f"ss_{content_type}_") as tmp_dir:
            screenshot_path = Path(tmp_dir) / f"{item_id or event_id}_browser.jpg"
            if await take_screenshot(share_url, screenshot_path):
                return await _analyze_image_content(
                    event_id, str(screenshot_path), file_name,
                    db_pool, ai_config, content_type,
                )

        reason = "Browser screenshot failed for sharing URL"
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {"status": "failed", "content_type": content_type, "reason": reason}

    except Exception as exc:
        reason = f"Unhandled error: {exc}"
        logger.exception("Inspection failed for event %s", event_id)
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {
            "status": "failed",
            "content_type": content_type,
            "summary": None,
            "categories": [],
            "reason": reason,
        }


# ---------------------------------------------------------------------------
# Image preprocessing (also imported by browser_fetcher)
# ---------------------------------------------------------------------------

def _preprocess_image_bytes(img_bytes: bytes, max_edge: int = 1600, quality: int = 85) -> bytes:
    """Resize and compress image bytes (PNG/JPEG) for multimodal AI.

    Returns JPEG bytes with longest edge <= max_edge and quality setting applied.
    """
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(img_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize if larger than max_edge
    w, h = img.size
    if max(w, h) > max_edge:
        ratio = max_edge / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Analysis helper
# ---------------------------------------------------------------------------

async def _analyze_image_content(
    event_id: str,
    image_path: str,
    file_name: str,
    db_pool,
    ai_config: dict,
    content_type: str,
) -> dict:
    """Run multimodal (image) analysis, save verdict, mark event complete."""
    from .ai_bridge import analyze_image, save_verdict

    result = await analyze_image(image_path, file_name, content_type, ai_config)
    verdict_id = await save_verdict(event_id, result, "multimodal", db_pool)
    if verdict_id is None:
        reason = "Failed to save AI verdict to database"
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {"status": "failed", "content_type": content_type, "reason": reason}
    await _mark_inspection_complete(event_id, db_pool)

    return {
        "status": "completed",
        "content_type": content_type,
        "summary": result.get("summary"),
        "categories": [c.get("id") for c in result.get("categories", [])],
    }


# ---------------------------------------------------------------------------
# Status updates
# ---------------------------------------------------------------------------

async def _mark_inspection_complete(event_id: str, db_pool) -> None:
    """Update event status to completed after successful inspection."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET status = 'completed',
                    processing_completed_at = NOW()
                WHERE event_id = $1
                """,
                event_id,
            )
    except Exception:
        logger.exception("Failed to mark event %s as completed", event_id)


async def _mark_inspection_failed(event_id: str, db_pool, reason: str) -> None:
    """Update event status to inspection_failed with reason."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET status = 'inspection_failed',
                    failure_reason = $2
                WHERE event_id = $1
                """,
                event_id, reason,
            )
    except Exception:
        logger.exception("Failed to mark event %s as inspection_failed", event_id)
