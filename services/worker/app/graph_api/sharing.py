"""Sharing link retrieval via the Graph API permissions endpoint."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from .auth import GraphAuth
from .client import GRAPH_BASE, DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)


async def get_sharing_permissions(
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
    timeout: httpx.Timeout | None = None,
) -> List[Dict[str, Any]]:
    """Return the list of permission objects for a drive item.

    Calls ``GET /drives/{driveId}/items/{itemId}/permissions``.
    """
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/permissions"
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    _timeout = timeout or DEFAULT_TIMEOUT

    async with httpx.AsyncClient(timeout=_timeout) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("value", [])


def extract_sharing_link(permissions: List[Dict[str, Any]]) -> Optional[str]:
    """Find the first anonymous or organization-wide sharing link URL.

    Scans the permission entries for one whose ``link.scope`` is
    ``"anonymous"`` or ``"organization"`` and returns its ``webUrl``.
    Returns ``None`` when no matching link is found.
    """
    for perm in permissions:
        link = perm.get("link")
        if not link:
            continue
        scope = link.get("scope", "").lower()
        if scope in ("anonymous", "organization"):
            web_url = link.get("webUrl")
            if web_url:
                logger.debug("Found sharing link scope=%s url=%s", scope, web_url)
                return web_url
    return None


def extract_all_sharing_links(
    permissions: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Return all anonymous/org-wide sharing links with scope and permission labels.

    Each entry: ``{"url": "...", "scope": "...", "type": "...", "label": "OrgEdit"}``.
    """
    results: List[Dict[str, str]] = []
    for perm in permissions:
        link = perm.get("link")
        if not link:
            continue
        scope = link.get("scope", "").lower()
        if scope not in ("anonymous", "organization"):
            continue
        web_url = link.get("webUrl")
        if not web_url:
            continue
        link_type = link.get("type", "view").lower()
        label = scope.capitalize() + link_type.capitalize()
        results.append({
            "url": web_url,
            "scope": scope,
            "type": link_type,
            "label": label,
        })
    return results
