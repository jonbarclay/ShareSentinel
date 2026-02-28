"""Fetch OneNote notebook page content via Graph API (delegated)."""

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
    item_id: str,
    user_id: str,
    access_token: str,
    site_url: str | None = None,
) -> Optional[str]:
    """Fetch all page content from a OneNote notebook.

    Enumerates sections and pages, then fetches each page's HTML content.
    Concatenates all page text up to MAX_CONTENT_BYTES.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    notebooks = await _find_notebooks(user_id, headers)
    if not notebooks:
        logger.warning("No notebooks found for user %s", user_id)
        return None

    all_text: list[str] = []
    total_bytes = 0

    for notebook in notebooks:
        notebook_id = notebook["id"]
        sections = await _get_sections(notebook_id, headers, user_id)

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


async def _find_notebooks(user_id: str, headers: dict) -> list[dict]:
    url = f"{GRAPH_BASE}/users/{user_id}/onenote/notebooks?$top=10"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to list notebooks: HTTP %d", resp.status_code)
            return []
        return resp.json().get("value", [])


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
