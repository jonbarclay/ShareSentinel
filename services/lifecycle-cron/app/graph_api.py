"""Graph API authentication and permission removal.

Re-exports ``GraphAuth`` from the shared ``sharesentinel_common`` package.
Falls back to a local implementation if the shared package is not installed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Union

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


try:
    from sharesentinel_common.graph_auth import GraphAuth, GRAPH_SCOPE  # noqa: F401
except ImportError:
    import hashlib

    import msal
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        pkcs12,
    )

    GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

    class GraphAuth:
        """Acquires and caches Microsoft Graph API access tokens."""

        def __init__(
            self,
            tenant_id: str,
            client_id: str,
            client_secret: str,
            certificate_path: str | None = None,
            certificate_password: str | None = None,
        ) -> None:
            self._authority = f"https://login.microsoftonline.com/{tenant_id}"
            self._app: Optional[msal.ConfidentialClientApplication] = None
            self._client_id = client_id
            self._client_secret = client_secret
            self._certificate_path = certificate_path
            self._certificate_password = certificate_password

        def _resolve_credential(self) -> Union[str, Dict[str, str]]:
            if self._certificate_path:
                with open(self._certificate_path, "rb") as f:
                    pfx_data = f.read()
                private_key, certificate, _ = pkcs12.load_key_and_certificates(
                    pfx_data,
                    self._certificate_password.encode() if self._certificate_password else None,
                )
                pem_key = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
                thumbprint = hashlib.sha1(certificate.public_bytes(Encoding.DER)).hexdigest().upper()
                logger.info("Using certificate credential (thumbprint=%s)", thumbprint)
                return {"private_key": pem_key.decode(), "thumbprint": thumbprint}
            return self._client_secret

        def _get_app(self) -> msal.ConfidentialClientApplication:
            if self._app is None:
                self._app = msal.ConfidentialClientApplication(
                    client_id=self._client_id,
                    client_credential=self._resolve_credential(),
                    authority=self._authority,
                )
            return self._app

        def get_access_token(self) -> str:
            app = self._get_app()
            result = app.acquire_token_silent(GRAPH_SCOPE, account=None)
            if result and "access_token" in result:
                return result["access_token"]
            result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
            if result and "access_token" in result:
                return result["access_token"]
            error = result.get("error_description", result.get("error", "unknown error"))
            raise RuntimeError(f"Graph API token acquisition failed: {error}")


async def enumerate_folder_children(
    auth: GraphAuth,
    drive_id: str,
    folder_item_id: str,
    recursive: bool = True,
) -> list[dict]:
    """Enumerate all files in a folder via Graph API.

    Returns a flat list of driveItem dicts that have a ``file`` facet.
    Includes cTag/eTag for change detection.
    """
    accumulator: list[dict] = []
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

    async def _recurse(item_id: str) -> None:
        url: str | None = (
            f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children"
            "?$top=200&$select=id,name,size,file,folder,parentReference,webUrl,cTag,eTag,lastModifiedDateTime"
        )
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while url:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("value", []):
                    if "file" in item:
                        accumulator.append(item)
                    elif "folder" in item and recursive:
                        await _recurse(item["id"])
                url = data.get("@odata.nextLink")

    await _recurse(folder_item_id)
    logger.info(
        "Enumerated %d files in drive=%s item=%s",
        len(accumulator), drive_id[:12], folder_item_id[:12],
    )
    return accumulator


async def enumerate_m365_groups(auth: GraphAuth) -> list[dict]:
    """Fetch all Unified (M365) groups with visibility info.

    Returns list of dicts with keys: id, displayName, visibility,
    resourceProvisioningOptions.
    """
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    initial_params = {
        "$filter": "groupTypes/any(c:c eq 'Unified')",
        "$select": "id,displayName,visibility,mail,resourceProvisioningOptions",
        "$top": "999",
    }
    groups: list[dict] = []

    url: str | None = f"{GRAPH_BASE}/groups"
    first_page = True
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        while url:
            resp = await client.get(
                url,
                headers=headers,
                params=initial_params if first_page else None,
            )
            first_page = False
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                logger.warning("Graph 429 — sleeping %ds", retry_after)
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            data = resp.json()
            groups.extend(data.get("value", []))
            url = data.get("@odata.nextLink")

    logger.info("Enumerated %d M365 groups", len(groups))
    return groups


async def batch_get_group_sites(
    auth: GraphAuth,
    group_ids: list[str],
) -> dict[str, str]:
    """Resolve group IDs to SharePoint site URLs using Graph batch API.

    Returns dict mapping group_id -> site_url.
    Processes in batches of 20 (Graph batch limit).
    """
    result: dict[str, str] = {}
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }

    async def _run_batch(chunk: list[str]) -> list[str]:
        """POST one $batch of site lookups; returns ids worth retrying."""
        requests = [
            {
                "id": gid,
                "method": "GET",
                "url": f"/groups/{gid}/sites/root?$select=id,webUrl",
            }
            for gid in chunk
        ]
        batch_body = {"requests": requests}

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            for _attempt in range(4):
                resp = await client.post(
                    f"{GRAPH_BASE}/$batch",
                    headers=headers,
                    json=batch_body,
                )
                if resp.status_code != 429:
                    break
                retry_after = int(resp.headers.get("Retry-After", "10"))
                logger.warning("Graph batch 429 — sleeping %ds", retry_after)
                await asyncio.sleep(retry_after)
            resp.raise_for_status()
            data = resp.json()

        failed: list[str] = []
        for item in data.get("responses", []):
            status = item.get("status")
            if status == 200:
                body = item.get("body", {})
                web_url = body.get("webUrl", "")
                if web_url:
                    result[item["id"]] = web_url
            elif status == 404:
                # Group has no site (yet) — normal for very new groups.
                logger.debug("No site for group %s (404)", item.get("id"))
            else:
                # A failed lookup leaves the group's site URL unknown, which
                # silently skips the post-remediation policy cascade for that
                # site — surface it and retry once.
                logger.warning(
                    "Batch site lookup failed for group %s: HTTP %s",
                    item.get("id"), status,
                )
                if item.get("id"):
                    failed.append(item["id"])
        return failed

    retry_ids: list[str] = []
    for i in range(0, len(group_ids), 20):
        retry_ids.extend(await _run_batch(group_ids[i : i + 20]))
        await asyncio.sleep(0.2)  # Rate limit between batches

    if retry_ids:
        logger.info("Retrying %d failed group site lookups", len(retry_ids))
        await asyncio.sleep(5)
        still_failed: list[str] = []
        for i in range(0, len(retry_ids), 20):
            still_failed.extend(await _run_batch(retry_ids[i : i + 20]))
            await asyncio.sleep(0.2)
        if still_failed:
            logger.warning(
                "%d group site lookups failed after retry (policy cascade "
                "will be skipped for these groups this scan): %s",
                len(still_failed), still_failed[:10],
            )

    logger.info("Resolved %d/%d group site URLs", len(result), len(group_ids))
    return result


async def get_group_member_upns(
    auth: GraphAuth,
    group_id: str,
) -> set[str]:
    """Fetch userPrincipalNames of every DIRECT user member of an Entra ID group.

    Deliberately direct-only (/members, not /transitiveMembers): the CWS
    exception group is managed as a flat list of users by policy (decision
    2026-07-05) — nesting a group inside it does NOT confer exemption. The
    PowerShell scripts' Graph queries use the same shape; keep them in sync
    if this ever changes.

    Returns a set of lowercased UPNs. Raises on non-transient errors so the
    caller can fail-closed rather than remediate with a stale/empty set.
    """
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "ConsistencyLevel": "eventual",
    }
    upns: set[str] = set()

    url: str | None = f"{GRAPH_BASE}/groups/{group_id}/members/microsoft.graph.user"
    first_page = True
    initial_params = {
        "$select": "id,userPrincipalName",
        "$top": "999",
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        while url:
            resp = await client.get(
                url,
                headers=headers,
                params=initial_params if first_page else None,
            )
            first_page = False
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                logger.warning("Graph 429 on group members — sleeping %ds", retry_after)
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            data = resp.json()
            for m in data.get("value", []):
                upn = (m.get("userPrincipalName") or "").strip().lower()
                if upn:
                    upns.add(upn)
            url = data.get("@odata.nextLink")

    logger.info("Resolved %d UPNs from group %s", len(upns), group_id)
    return upns


# NOTE: an unused /sites/delta full-tenant enumerator (enumerate_all_sites)
# was removed 2026-07-05 — the site inventory now comes from PnP PowerShell
# (scripts/Get-SitePolicyInventory.ps1) which pages correctly at tenant scale.


async def get_group(auth: GraphAuth, group_id: str) -> dict | None:
    """Fetch a single group's policy-relevant properties.

    Returns the group dict (id, displayName, visibility, groupTypes) or None
    if the group does not exist (404 — e.g. deleted, or directory replication
    has not caught up yet). Honors 429 with a single retry.
    """
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    url = f"{GRAPH_BASE}/groups/{group_id}"
    params = {"$select": "id,displayName,visibility,groupTypes"}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            logger.warning("Graph 429 on group GET — sleeping %ds", retry_after)
            await asyncio.sleep(retry_after)
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def get_group_site_url(auth: GraphAuth, group_id: str) -> str:
    """Resolve a group's root SharePoint site webUrl. Empty string if absent
    (site provisioning lags group creation, sometimes by minutes)."""
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    url = f"{GRAPH_BASE}/groups/{group_id}/sites/root"
    params = {"$select": "id,webUrl"}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return ""
        return resp.json().get("webUrl", "") or ""


async def set_group_visibility(
    auth: GraphAuth,
    group_id: str,
    visibility: str,
) -> None:
    """Set an M365 group's visibility (e.g., 'Private').

    Uses PATCH /groups/{group_id}.
    """
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }
    url = f"{GRAPH_BASE}/groups/{group_id}"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.patch(url, headers=headers, json={"visibility": visibility})
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            logger.warning("Graph 429 on group PATCH — sleeping %ds", retry_after)
            await asyncio.sleep(retry_after)
            resp = await client.patch(url, headers=headers, json={"visibility": visibility})
        resp.raise_for_status()

    logger.info("Set group %s visibility to %s", group_id, visibility)


async def get_item_permissions(
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
) -> list[dict]:
    """Fetch all permissions on a drive item."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/permissions"
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("value", [])


async def remove_sharing_permission(
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
    permission_id: str,
) -> bool:
    """DELETE a single permission from a drive item.

    Returns True on success (204) or if already removed (404).
    """
    url = (
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
        f"/permissions/{permission_id}"
    )
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.delete(url, headers=headers)
        if resp.status_code in (204, 404):
            return True
        resp.raise_for_status()
        return True
