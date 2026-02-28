"""Fetch Loop component content via Graph API HTML conversion."""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)


async def fetch_loop_content(
    drive_id: str, item_id: str, access_token: str,
) -> Optional[str]:
    """Download a Loop/Fluid file converted to HTML via Graph API.

    Uses the ``?format=html`` query parameter on the driveItem content endpoint.
    Returns the HTML string, or None on failure.
    """
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content?format=html"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type or "text" in content_type:
                return resp.text
            try:
                return resp.content.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Loop content is binary, cannot extract text (drive=%s, item=%s)", drive_id, item_id)
                return None
        elif resp.status_code == 406:
            logger.warning("HTML conversion not supported for Loop item %s, trying raw", item_id)
            return await _try_raw_download(drive_id, item_id, access_token)
        else:
            logger.error("Loop fetch failed: HTTP %d for item %s", resp.status_code, item_id)
            return None


async def _try_raw_download(
    drive_id: str, item_id: str, access_token: str,
) -> Optional[str]:
    """Fallback: download the raw file and attempt to extract readable text."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return None
