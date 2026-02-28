"""Fetch Whiteboard content via Graph API (delegated).

Since Whiteboards are now stored in OneDrive, we attempt:
1. PDF conversion via content?format=pdf
2. Thumbnail image as fallback
"""

import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)


async def fetch_whiteboard_as_pdf(
    drive_id: str, item_id: str, access_token: str, dest_dir: Path,
) -> Optional[Path]:
    """Download a Whiteboard file converted to PDF.

    Returns the path to the saved PDF file, or None on failure.
    """
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content?format=pdf"
    headers = {"Authorization": f"Bearer {access_token}"}

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{item_id}.pdf"

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            dest_path.write_bytes(resp.content)
            logger.info("Whiteboard PDF saved: %s (%d bytes)", dest_path, len(resp.content))
            return dest_path
        elif resp.status_code == 406:
            logger.warning("PDF conversion not supported for whiteboard %s", item_id)
            return None
        else:
            logger.error("Whiteboard PDF fetch failed: HTTP %d for item %s", resp.status_code, item_id)
            return None


async def fetch_whiteboard_as_image(
    drive_id: str, item_id: str, access_token: str, dest_dir: Path,
) -> Optional[Path]:
    """Download a Whiteboard thumbnail/preview as PNG.

    Falls back to the driveItem thumbnail endpoint if PDF conversion fails.
    """
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/thumbnails/0/large/content"
    headers = {"Authorization": f"Bearer {access_token}"}

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{item_id}.png"

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            dest_path.write_bytes(resp.content)
            logger.info("Whiteboard image saved: %s (%d bytes)", dest_path, len(resp.content))
            return dest_path
        else:
            logger.error("Whiteboard image fetch failed: HTTP %d", resp.status_code)
            return None
