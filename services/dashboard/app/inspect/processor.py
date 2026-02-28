"""Orchestrate inspection processing for a single event.

Handles Loop, OneNote, and Whiteboard content types by fetching content
via Graph API delegated tokens, running AI analysis, and saving results.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


async def process_inspection_item(
    event: dict,
    access_token: str,
    db_pool,
    ai_config: dict,
) -> dict:
    """Process a single inspection item end-to-end.

    Args:
        event: Database row dict with event_id, content_type, drive_id,
               item_id, user_id, site_url, file_name, etc.
        access_token: Graph API delegated access token.
        db_pool: asyncpg connection pool.
        ai_config: Dict with provider, api_key, model, max_tokens.

    Returns:
        Dict with status, content_type, summary, categories.
    """
    event_id = event["event_id"]
    content_type = event.get("content_type", "unknown")
    file_name = event.get("file_name", "unknown")
    drive_id = event.get("drive_id")
    item_id = event.get("item_id")
    user_id = event.get("user_id")
    site_url = event.get("site_url")

    try:
        if content_type == "loop":
            return await _process_loop(
                event_id, drive_id, item_id, file_name,
                access_token, db_pool, ai_config,
            )
        elif content_type == "onenote":
            return await _process_onenote(
                event_id, item_id, user_id, site_url, file_name,
                access_token, db_pool, ai_config,
            )
        elif content_type == "whiteboard":
            return await _process_whiteboard(
                event_id, drive_id, item_id, file_name,
                access_token, db_pool, ai_config,
            )
        else:
            reason = f"Unsupported content_type: {content_type}"
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


async def _process_loop(
    event_id: str,
    drive_id: str,
    item_id: str,
    file_name: str,
    access_token: str,
    db_pool,
    ai_config: dict,
) -> dict:
    """Fetch Loop content and analyze as text."""
    from .loop_fetcher import fetch_loop_content

    content = await fetch_loop_content(drive_id, item_id, access_token)
    if not content:
        reason = "Failed to fetch Loop content (empty or error)"
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {"status": "failed", "content_type": "loop", "reason": reason}

    return await _analyze_text_content(
        event_id, content, file_name, db_pool, ai_config, "loop",
    )


async def _process_onenote(
    event_id: str,
    item_id: str,
    user_id: str,
    site_url: Optional[str],
    file_name: str,
    access_token: str,
    db_pool,
    ai_config: dict,
) -> dict:
    """Fetch OneNote content and analyze as text."""
    from .onenote_fetcher import fetch_onenote_content

    content = await fetch_onenote_content(item_id, user_id, access_token, site_url)
    if not content:
        reason = "Failed to fetch OneNote content (empty or error)"
        await _mark_inspection_failed(event_id, db_pool, reason)
        return {"status": "failed", "content_type": "onenote", "reason": reason}

    return await _analyze_text_content(
        event_id, content, file_name, db_pool, ai_config, "onenote",
    )


async def _process_whiteboard(
    event_id: str,
    drive_id: str,
    item_id: str,
    file_name: str,
    access_token: str,
    db_pool,
    ai_config: dict,
) -> dict:
    """Fetch Whiteboard content: try PDF first, then image fallback."""
    from .whiteboard_fetcher import fetch_whiteboard_as_pdf, fetch_whiteboard_as_image

    with tempfile.TemporaryDirectory(prefix="ss_wb_") as tmp_dir:
        dest_dir = Path(tmp_dir)

        # Try PDF conversion first
        pdf_path = await fetch_whiteboard_as_pdf(drive_id, item_id, access_token, dest_dir)
        if pdf_path and pdf_path.exists():
            return await _analyze_file_content(
                event_id, str(pdf_path), file_name, db_pool, ai_config, "whiteboard",
            )

        # Fall back to image
        image_path = await fetch_whiteboard_as_image(drive_id, item_id, access_token, dest_dir)
        if image_path and image_path.exists():
            return await _analyze_image_content(
                event_id, str(image_path), file_name, db_pool, ai_config, "whiteboard",
            )

    reason = "Failed to fetch Whiteboard content (no PDF or image available)"
    await _mark_inspection_failed(event_id, db_pool, reason)
    return {"status": "failed", "content_type": "whiteboard", "reason": reason}


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

async def _analyze_text_content(
    event_id: str,
    text: str,
    file_name: str,
    db_pool,
    ai_config: dict,
    content_type: str,
) -> dict:
    """Run text analysis, save verdict, mark event complete."""
    from .ai_bridge import analyze_text, save_verdict

    result = await analyze_text(text, file_name, content_type, ai_config)
    await save_verdict(event_id, result, "text", db_pool)
    await _mark_inspection_complete(event_id, db_pool)

    return {
        "status": "completed",
        "content_type": content_type,
        "summary": result.get("summary"),
        "categories": [c.get("id") for c in result.get("categories", [])],
    }


async def _analyze_file_content(
    event_id: str,
    file_path: str,
    file_name: str,
    db_pool,
    ai_config: dict,
    content_type: str,
) -> dict:
    """Extract text from a PDF file, then analyze. Falls back to image analysis."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(file_path)
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        text = "\n".join(text_parts).strip()
    except Exception:
        logger.warning("PyMuPDF text extraction failed for %s, trying image analysis", file_path)
        text = ""

    if text:
        return await _analyze_text_content(
            event_id, text, file_name, db_pool, ai_config, content_type,
        )

    # Fall back to image-based analysis of the PDF
    return await _analyze_image_content(
        event_id, file_path, file_name, db_pool, ai_config, content_type,
    )


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
    await save_verdict(event_id, result, "multimodal", db_pool)
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
