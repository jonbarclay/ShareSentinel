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
) -> List[Dict[str, Any]]:
    """Return all anonymous/org-wide sharing links with scope, permission labels, and metadata.

    Each entry includes ``permission_id`` and ``expiration_date`` from the
    Graph API permission object (both may be None).
    """
    results: List[Dict[str, Any]] = []
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
        label = scope.capitalize() + " " + link_type.capitalize()
        expiration = perm.get("expirationDateTime")
        # Normalize the Graph sentinel "0001-01-01T00:00:00Z" to None
        if expiration and str(expiration).startswith("0001-01-01"):
            expiration = None
        results.append({
            "url": web_url,
            "scope": scope,
            "type": link_type,
            "label": label,
            "permission_id": perm.get("id"),
            "expiration_date": expiration,
        })
    return results


def has_active_sharing_links(
    sharing_links: Optional[List[Dict[str, Any]]],
) -> bool:
    """Return True if at least one sharing link is present and not expired.

    A link is considered active if it exists in the list (meaning the
    Graph API still reports it) AND either has no ``expiration_date`` or
    the ``expiration_date`` is in the future.

    Returns ``True`` (fail-open) when *sharing_links* is ``None`` — this
    means the sharing state could not be determined (e.g. permissions
    fetch failed), so processing should NOT be skipped.
    """
    if sharing_links is None:
        return True  # fail-open: unknown state, assume active

    if not sharing_links:
        return False  # empty list: all links have been removed

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for link in sharing_links:
        expiration = link.get("expiration_date")
        if expiration is None:
            return True  # no expiration = active
        try:
            exp_dt = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
            if exp_dt > now:
                return True  # expiration in the future = active
        except (ValueError, TypeError):
            return True  # unparseable expiration = fail-open
    return False  # all links expired
