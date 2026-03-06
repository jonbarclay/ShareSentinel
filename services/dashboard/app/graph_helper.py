"""Minimal Graph API helper for SharePoint site search.

Uses the same certificate-based auth pattern as the lifecycle-cron service.
"""

import hashlib
import logging
from typing import Dict, List, Optional, Union

import httpx
import msal
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

from .config import (
    AZURE_TENANT_ID,
    AZURE_CLIENT_ID,
    AZURE_CERTIFICATE,
    AZURE_CERTIFICATE_PASS,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_msal_app: Optional[msal.ConfidentialClientApplication] = None


def _resolve_credential() -> Union[str, Dict[str, str]]:
    if AZURE_CERTIFICATE:
        with open(AZURE_CERTIFICATE, "rb") as f:
            pfx_data = f.read()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_data,
            AZURE_CERTIFICATE_PASS.encode() if AZURE_CERTIFICATE_PASS else None,
        )
        pem_key = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        thumbprint = hashlib.sha1(certificate.public_bytes(Encoding.DER)).hexdigest().upper()
        logger.info("Using certificate credential (thumbprint=%s)", thumbprint)
        return {"private_key": pem_key.decode(), "thumbprint": thumbprint}
    return ""


def _get_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=AZURE_CLIENT_ID,
            client_credential=_resolve_credential(),
            authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        )
    return _msal_app


def _get_access_token() -> str:
    app = _get_app()
    result = app.acquire_token_silent(GRAPH_SCOPE, account=None)
    if result and "access_token" in result:
        return result["access_token"]
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if result and "access_token" in result:
        return result["access_token"]
    error = result.get("error_description", result.get("error", "unknown error"))
    raise RuntimeError(f"Graph API token acquisition failed: {error}")


def is_configured() -> bool:
    """Return True if Graph API credentials are configured."""
    return bool(AZURE_TENANT_ID and AZURE_CLIENT_ID and AZURE_CERTIFICATE)


async def search_sites(query: str, top: int = 20) -> List[Dict]:
    """Search SharePoint sites via Graph API.

    Returns list of {id, displayName, webUrl} dicts.
    """
    if not is_configured():
        logger.warning("Graph API not configured — site search unavailable")
        return []

    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "search": query,
        "$select": "id,displayName,webUrl",
        "$top": str(top),
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/sites",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "id": site["id"],
            "displayName": site.get("displayName", ""),
            "webUrl": site.get("webUrl", ""),
        }
        for site in data.get("value", [])
    ]


async def search_m365_groups(query: str, top: int = 20) -> List[Dict]:
    """Search M365 (Unified) groups by display name via Graph API.

    Returns list of {id, displayName, visibility, siteUrl} dicts.

    Uses three strategies in sequence:
    1. $search on displayName (index-based, fast but can lag for new groups)
    2. startsWith filter on displayName (no index dependency, finds new groups)
    3. SharePoint site search → resolve owning group (catches groups by site name)

    Results from all strategies are merged and deduplicated.
    """
    if not is_configured():
        logger.warning("Graph API not configured — group search unavailable")
        return []

    token = _get_access_token()
    seen_ids: set = set()
    groups: List[Dict] = []

    # Strategy 1: $search (index-based)
    try:
        search_groups = await _search_groups_by_search(token, query, top)
        for g in search_groups:
            if g["id"] not in seen_ids:
                seen_ids.add(g["id"])
                groups.append(g)
    except Exception as e:
        logger.warning("Group $search failed, continuing with fallbacks: %s", e)

    # Strategy 2: startsWith filter (no index lag)
    try:
        filter_groups = await _search_groups_by_filter(token, query, top)
        for g in filter_groups:
            if g["id"] not in seen_ids:
                seen_ids.add(g["id"])
                groups.append(g)
    except Exception as e:
        logger.warning("Group $filter search failed: %s", e)

    # Strategy 3: SharePoint site search → resolve owning group
    if len(groups) < top:
        try:
            site_groups = await _search_groups_via_sites(token, query, top - len(groups))
            for g in site_groups:
                if g["id"] not in seen_ids:
                    seen_ids.add(g["id"])
                    groups.append(g)
        except Exception as e:
            logger.warning("Site-based group search failed: %s", e)

    # Resolve site URLs for any groups that don't have one yet
    for group in groups:
        if not group.get("siteUrl"):
            try:
                group["siteUrl"] = await _resolve_group_site_url(token, group["id"])
            except Exception:
                pass

    return groups[:top]


async def _search_groups_by_search(token: str, query: str, top: int) -> List[Dict]:
    """Search groups using $search (index-based)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    params = {
        "$filter": "groupTypes/any(c:c eq 'Unified')",
        "$search": f'"displayName:{query}"',
        "$select": "id,displayName,visibility",
        "$top": str(top),
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/groups", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    return [
        {"id": g["id"], "displayName": g.get("displayName", ""),
         "visibility": g.get("visibility", ""), "siteUrl": ""}
        for g in data.get("value", [])
    ]


async def _search_groups_by_filter(token: str, query: str, top: int) -> List[Dict]:
    """Search groups using startsWith filter (no index dependency)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    # Escape single quotes in query for OData filter
    safe_query = query.replace("'", "''")
    params = {
        "$filter": f"groupTypes/any(c:c eq 'Unified') and startsWith(displayName, '{safe_query}')",
        "$select": "id,displayName,visibility",
        "$top": str(top),
        "$count": "true",
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/groups", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    return [
        {"id": g["id"], "displayName": g.get("displayName", ""),
         "visibility": g.get("visibility", ""), "siteUrl": ""}
        for g in data.get("value", [])
    ]


async def _search_groups_via_sites(token: str, query: str, top: int) -> List[Dict]:
    """Search SharePoint sites, then resolve the owning M365 group for each.

    Useful when group search index hasn't caught up for newly created groups.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "search": query,
        "$select": "id,displayName,webUrl",
        "$top": str(top),
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/sites", headers=headers, params=params)
        resp.raise_for_status()
        sites = resp.json().get("value", [])

    groups: List[Dict] = []
    for site in sites:
        site_url = site.get("webUrl", "")
        if not site_url or "/personal/" in site_url.lower():
            continue

        # Try to resolve the owning group by mailNickname derived from the site URL path
        group = await _resolve_group_from_site_url(token, site_url)
        if group:
            group["siteUrl"] = site_url
            groups.append(group)

    return groups


async def _resolve_group_from_site_url(token: str, site_url: str) -> Optional[Dict]:
    """Try to find the M365 group that owns a SharePoint site.

    Extracts the site path segment and looks for a Unified group with a matching mailNickname.
    """
    # Extract site path: "https://tenant.sharepoint.com/sites/SiteName" -> "SiteName"
    import re
    match = re.search(r"/sites/([^/?#]+)", site_url)
    if not match:
        return None
    site_nickname = match.group(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    safe_nickname = site_nickname.replace("'", "''")
    params = {
        "$filter": f"mailNickname eq '{safe_nickname}' and groupTypes/any(c:c eq 'Unified')",
        "$select": "id,displayName,visibility",
        "$top": "1",
        "$count": "true",
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/groups", headers=headers, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()

    values = data.get("value", [])
    if not values:
        return None

    g = values[0]
    return {
        "id": g["id"],
        "displayName": g.get("displayName", ""),
        "visibility": g.get("visibility", ""),
        "siteUrl": "",
    }


async def _resolve_group_site_url(token: str, group_id: str) -> str:
    """Resolve a single group's root site URL."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/groups/{group_id}/sites/root",
            headers=headers,
            params={"$select": "webUrl"},
        )
        if resp.status_code == 200:
            return resp.json().get("webUrl", "")
    return ""


async def get_site_details(
    site_url: str,
    group_id: str = "",
) -> Dict:
    """Fetch detailed info about a site/group for the allow list detail panel.

    Returns dict with: visibility, sharing_capability, owners, member_count, group_id,
    description, created_datetime.
    """
    if not is_configured():
        return {"error": "Graph API not configured"}

    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    result: Dict = {
        "group_id": group_id,
        "site_url": site_url,
        "visibility": "",
        "description": "",
        "created_datetime": "",
        "sharing_capability": "",
        "owners": [],
        "members": [],
        "member_count": 0,
    }

    # If we don't have a group_id, try to resolve it from the site URL
    if not group_id and site_url:
        resolved = await _resolve_group_from_site_url(token, site_url)
        if resolved:
            group_id = resolved["id"]
            result["group_id"] = group_id

    if not group_id:
        return result

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        # Fetch group properties
        resp = await client.get(
            f"{GRAPH_BASE}/groups/{group_id}",
            headers=headers,
            params={"$select": "id,displayName,visibility,description,createdDateTime,mail"},
        )
        if resp.status_code == 200:
            g = resp.json()
            result["visibility"] = g.get("visibility", "")
            result["description"] = g.get("description", "") or ""
            result["created_datetime"] = g.get("createdDateTime", "")
            result["mail"] = g.get("mail", "")

        # Fetch owners (typically a small list)
        resp = await client.get(
            f"{GRAPH_BASE}/groups/{group_id}/owners",
            headers=headers,
            params={"$select": "id,displayName,mail,userPrincipalName", "$top": "50"},
        )
        if resp.status_code == 200:
            result["owners"] = [
                {
                    "displayName": o.get("displayName", ""),
                    "mail": o.get("mail", "") or o.get("userPrincipalName", ""),
                }
                for o in resp.json().get("value", [])
            ]

        # Fetch members (first page + count)
        resp = await client.get(
            f"{GRAPH_BASE}/groups/{group_id}/members",
            headers={**headers, "ConsistencyLevel": "eventual"},
            params={
                "$select": "id,displayName,mail,userPrincipalName",
                "$top": "20",
                "$count": "true",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            result["member_count"] = data.get("@odata.count", len(data.get("value", [])))
            result["members"] = [
                {
                    "displayName": m.get("displayName", ""),
                    "mail": m.get("mail", "") or m.get("userPrincipalName", ""),
                }
                for m in data.get("value", [])
            ]

    return result
