"""Fetch OneNote notebook page content via Graph API (delegated).

Scoped to the specific shared notebook identified by its driveItem ID,
rather than enumerating all of a user's notebooks.
"""

import logging
from typing import Optional

import httpx
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
MAX_PAGES = 50
MAX_CONTENT_BYTES = 100_000


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text converter for OneNote page content."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip = True
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


async def fetch_onenote_content(
    drive_id: str,
    item_id: str,
    user_id: str,
    access_token: str,
    site_url: str | None = None,
) -> Optional[str]:
    """Fetch page content from a specific shared OneNote notebook.

    Uses the driveItem ID to resolve the notebook, then enumerates only
    that notebook's sections and pages. Returns None if the notebook
    cannot be identified (fail-safe).
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    # Step 1: Resolve the driveItem to get notebook metadata
    notebook_name = await _resolve_notebook_name(drive_id, item_id, headers)
    if not notebook_name:
        logger.warning(
            "Could not resolve OneNote notebook from drive_id=%s item_id=%s",
            drive_id, item_id,
        )
        return None

    # Step 2: Find the matching notebook via OneNote API
    notebook = await _find_notebook_by_name(user_id, notebook_name, headers)
    if not notebook:
        logger.warning(
            "No matching OneNote notebook found for name '%s' (user %s)",
            notebook_name, user_id,
        )
        return None

    # Step 3: Enumerate only this notebook's sections and pages
    notebook_id = notebook["id"]
    sections = await _get_sections(notebook_id, headers, user_id)

    all_text: list[str] = []
    total_bytes = 0

    for section in sections:
        pages = await _get_pages(section["id"], headers, user_id)

        for page in pages[:MAX_PAGES]:
            page_html = await _get_page_content(page["id"], headers, user_id)
            if page_html:
                page_text = _html_to_text(page_html)
                if page_text:
                    all_text.append(f"--- Page: {page.get('title', 'Untitled')} ---\n{page_text}")
                    total_bytes += len(page_text.encode("utf-8"))
                    if total_bytes >= MAX_CONTENT_BYTES:
                        logger.info("OneNote content limit reached (%d bytes)", total_bytes)
                        return "\n\n".join(all_text)

    return "\n\n".join(all_text) if all_text else None


async def _resolve_notebook_name(
    drive_id: str, item_id: str, headers: dict
) -> Optional[str]:
    """Get the notebook name from its driveItem metadata.

    Checks that the item is a OneNote package and returns its display name.
    """
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error(
                "Failed to get driveItem metadata: HTTP %d (drive=%s, item=%s)",
                resp.status_code, drive_id, item_id,
            )
            return None
        data = resp.json()
        # OneNote notebooks have package.type == "oneNote"
        package = data.get("package", {})
        if package.get("type") != "oneNote":
            logger.warning(
                "DriveItem %s is not a OneNote package (package=%s)",
                item_id, package,
            )
            return None
        return data.get("name")


async def _find_notebook_by_name(
    user_id: str, name: str, headers: dict
) -> Optional[dict]:
    """Find a specific notebook by display name via the OneNote API."""
    # OData filter on displayName
    safe_name = name.replace("'", "''")
    url = (
        f"{GRAPH_BASE}/users/{user_id}/onenote/notebooks"
        f"?$filter=displayName eq '{safe_name}'"
        f"&$top=1"
    )
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to search notebooks by name: HTTP %d", resp.status_code)
            return None
        notebooks = resp.json().get("value", [])
        return notebooks[0] if notebooks else None


async def _get_sections(notebook_id: str, headers: dict, user_id: str) -> list[dict]:
    url = f"{GRAPH_BASE}/users/{user_id}/onenote/notebooks/{notebook_id}/sections"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        return resp.json().get("value", [])


async def _get_pages(section_id: str, headers: dict, user_id: str) -> list[dict]:
    url = f"{GRAPH_BASE}/users/{user_id}/onenote/sections/{section_id}/pages?$top={MAX_PAGES}&$select=id,title"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        return resp.json().get("value", [])


async def _get_page_content(page_id: str, headers: dict, user_id: str) -> Optional[str]:
    url = f"{GRAPH_BASE}/users/{user_id}/onenote/pages/{page_id}/content"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        return resp.text
