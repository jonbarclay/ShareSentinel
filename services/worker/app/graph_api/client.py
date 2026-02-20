"""Microsoft Graph API client for file metadata and downloads."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx

from .auth import GraphAuth

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Timeout: 30 s connect, 120 s read (large file downloads)
DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)


class GraphAPIError(Exception):
    """Raised for non-retryable Graph API errors."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class FileNotFoundError(GraphAPIError):
    """The requested item no longer exists (HTTP 404)."""


class AccessDeniedError(GraphAPIError):
    """The app lacks permissions to access the item (HTTP 403)."""


class GraphClient:
    """Wraps Microsoft Graph API calls for ShareSentinel.

    Parameters
    ----------
    auth:
        A ``GraphAuth`` instance for obtaining access tokens.
    timeout:
        Optional httpx timeout override.
    """

    def __init__(self, auth: GraphAuth, timeout: httpx.Timeout | None = None) -> None:
        self._auth = auth
        self._timeout = timeout or DEFAULT_TIMEOUT

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._auth.get_access_token()}"}

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 404:
            raise FileNotFoundError("Item not found", status_code=404)
        if response.status_code == 403:
            # Microsoft returns 403 sharesAccessDenied with HRESULT
            # 0x80070002 ("The system cannot find the file specified")
            # when the shared item has been deleted.  Treat as not-found
            # so the pipeline skips analysis instead of escalating.
            try:
                body = response.json()
                msg = body.get("error", {}).get("message", "")
                if "cannot find the file" in msg.lower() or "0x80070002" in msg:
                    raise FileNotFoundError(
                        f"Item no longer exists (403: {msg})", status_code=403,
                    )
            except (ValueError, KeyError):
                pass
            raise AccessDeniedError("Access denied", status_code=403)
        response.raise_for_status()

    @staticmethod
    def _site_id_from_url(site_url: str) -> str:
        """Extract a Graph-compatible site identifier from a SharePoint URL.

        The Graph API accepts ``{hostname}:/{server-relative-path}`` as
        the site identifier for ``/sites/{siteId}``.
        """
        parsed = urlparse(site_url)
        hostname = parsed.hostname or ""
        # Strip leading slash for the path portion
        path = parsed.path.rstrip("/")
        return f"{hostname}:{path}:" if path else hostname

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_sharing_url(url: str) -> str:
        """Encode a sharing/file URL for the Graph ``/shares`` endpoint.

        See https://learn.microsoft.com/en-us/graph/api/shares-get
        """
        encoded = base64.urlsafe_b64encode(url.encode()).decode()
        return "u!" + encoded.rstrip("=")

    async def get_item_metadata(
        self,
        object_id: str,
        site_url: str | None = None,
        workload: str | None = None,
        user_id: str | None = None,
        relative_path: str | None = None,
        file_name: str | None = None,
    ) -> Dict[str, Any]:
        """Retrieve drive-item metadata from the Graph API.

        Uses the ``/shares`` endpoint with the encoded ``object_id`` URL,
        which works universally for OneDrive and SharePoint items.

        Returns the full JSON response dict from Graph.

        Raises ``FileNotFoundError`` (404) or ``AccessDeniedError`` (403).
        """
        share_token = self._encode_sharing_url(object_id)
        url = f"{GRAPH_BASE}/shares/{share_token}/driveItem"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 400:
                # Log the response body for debugging
                from ..utils.log_sanitizer import sanitize_response_body
                logger.warning("Graph 400 response: %s", sanitize_response_body(resp.text))
            self._raise_for_status(resp)
            return resp.json()

    async def download_file(self, drive_id: str, item_id: str, dest_path: Path) -> Path:
        """Stream-download a file to *dest_path* on the tmpfs mount.

        Creates parent directories as needed. Returns the final path.

        Raises ``FileNotFoundError``, ``AccessDeniedError``, or
        ``httpx.HTTPStatusError`` on failure.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=self._headers()) as resp:
                if resp.status_code in (301, 302):
                    # Graph returns a redirect to the actual download URL
                    redirect_url = resp.headers.get("Location", "")
                    async with client.stream("GET", redirect_url) as dl_resp:
                        dl_resp.raise_for_status()
                        with open(dest_path, "wb") as f:
                            async for chunk in dl_resp.aiter_bytes(chunk_size=65_536):
                                f.write(chunk)
                else:
                    self._raise_for_status(resp)
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65_536):
                            f.write(chunk)

        logger.info("Downloaded %s (%d bytes)", dest_path.name, dest_path.stat().st_size)
        return dest_path

    async def download_file_converted(
        self,
        drive_id: str,
        item_id: str,
        dest_path: Path,
        output_format: str,
    ) -> Path:
        """Download a file with server-side format conversion.

        Uses ``GET /drives/{driveId}/items/{itemId}/content?format={fmt}``
        to convert Loop (→ HTML) and Whiteboard (→ PDF) files.

        Parameters
        ----------
        drive_id:
            The Graph API drive ID.
        item_id:
            The Graph API item ID.
        dest_path:
            Local path for the converted output.
        output_format:
            Target format — ``"html"`` or ``"pdf"``.

        Returns the final path on success.

        Raises ``ValueError`` for unsupported formats, ``GraphAPIError``
        for conversion failures (406/501), and the usual
        ``FileNotFoundError`` / ``AccessDeniedError`` on 404/403.
        """
        if output_format not in ("html", "pdf"):
            raise ValueError(f"Unsupported conversion format: {output_format!r} (expected 'html' or 'pdf')")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content?format={output_format}"

        # Longer timeout for server-side conversion
        conversion_timeout = httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0)

        async with httpx.AsyncClient(timeout=conversion_timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=self._headers()) as resp:
                if resp.status_code in (406, 501):
                    raise GraphAPIError(
                        f"Format conversion to {output_format} not supported for this item "
                        f"(HTTP {resp.status_code})",
                        status_code=resp.status_code,
                    )
                if resp.status_code in (301, 302):
                    redirect_url = resp.headers.get("Location", "")
                    async with client.stream("GET", redirect_url) as dl_resp:
                        dl_resp.raise_for_status()
                        with open(dest_path, "wb") as f:
                            async for chunk in dl_resp.aiter_bytes(chunk_size=65_536):
                                f.write(chunk)
                else:
                    self._raise_for_status(resp)
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65_536):
                            f.write(chunk)

        logger.info(
            "Downloaded converted %s (%d bytes, format=%s)",
            dest_path.name, dest_path.stat().st_size, output_format,
        )
        return dest_path

    async def list_folder_children(
        self, drive_id: str, item_id: str, recursive: bool = True,
    ) -> List[Dict[str, Any]]:
        """Enumerate all files in a folder (and subfolders if *recursive*).

        Returns a flat list of Graph driveItem dicts that have a ``file`` facet.
        Folders themselves are not included in the result.
        """
        accumulator: List[Dict[str, Any]] = []
        await self._enumerate_children(drive_id, item_id, accumulator, recursive)
        logger.info(
            "Enumerated %d files in drive=%s item=%s (recursive=%s)",
            len(accumulator), drive_id, item_id, recursive,
        )
        return accumulator

    async def _enumerate_children(
        self,
        drive_id: str,
        folder_item_id: str,
        accumulator: List[Dict[str, Any]],
        recursive: bool,
    ) -> None:
        """Recursively list children, accumulating file items."""
        url: str | None = (
            f"{GRAPH_BASE}/drives/{drive_id}/items/{folder_item_id}/children"
            "?$top=200&$select=id,name,size,file,folder,parentReference,webUrl"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while url:
                resp = await client.get(url, headers=self._headers())
                self._raise_for_status(resp)
                data = resp.json()
                for item in data.get("value", []):
                    if "file" in item:
                        accumulator.append(item)
                    elif "folder" in item and recursive:
                        child_id = item["id"]
                        await self._enumerate_children(
                            drive_id, child_id, accumulator, recursive,
                        )
                url = data.get("@odata.nextLink")

    async def get_site_owners(self, site_url: str) -> List[Dict[str, Any]]:
        """Fetch the owners of a SharePoint site.

        Strategy: resolve the site to get its associated Microsoft 365
        group ID, then call ``GET /groups/{groupId}/owners``.

        Returns a list of user dicts (displayName, mail, id).
        Returns an empty list on any error.
        """
        site_id = self._site_id_from_url(site_url)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # Step 1: Get the site to resolve the full Graph site ID
                site_resp = await client.get(
                    f"{GRAPH_BASE}/sites/{site_id}?$select=id,displayName",
                    headers=self._headers(),
                )
                if site_resp.status_code >= 400:
                    logger.warning(
                        "Graph API %d looking up site %s: %s",
                        site_resp.status_code, site_url, site_resp.text[:200],
                    )
                    return []

                site_data = site_resp.json()
                # The group info may be in the response directly or we need
                # to query the site's associated group via the root site ID
                graph_site_id = site_data.get("id", "")

                # Step 2: Try to get the group owners via /sites/{id}/owners
                # (available in some tenants) or fall back to group lookup
                owners_resp = await client.get(
                    f"{GRAPH_BASE}/sites/{graph_site_id}/permissions"
                    "?$filter=roles/any(r:r eq 'owner')"
                    "&$select=id,roles,grantedToV2",
                    headers=self._headers(),
                )
                if owners_resp.status_code < 400:
                    perms = owners_resp.json().get("value", [])
                    owners = []
                    for perm in perms:
                        granted = perm.get("grantedToV2", {})
                        user = granted.get("user", {})
                        if user.get("email") or user.get("displayName"):
                            owners.append({
                                "mail": user.get("email", ""),
                                "displayName": user.get("displayName", ""),
                                "id": user.get("id", ""),
                            })
                    if owners:
                        return owners

                logger.debug("No owners found via permissions for site %s", site_url)
                return []

        except Exception as exc:
            logger.warning("Failed to get site owners for %s: %s", site_url, exc)
            return []

    async def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """Fetch user profile from Graph API.

        Returns dict with displayName, jobTitle, department, mail.
        """
        url = f"{GRAPH_BASE}/users/{user_id}?$select=displayName,jobTitle,department,mail"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            self._raise_for_status(resp)
            return resp.json()

    async def get_user_manager(self, user_id: str) -> Dict[str, Any] | None:
        """Fetch user's manager from Graph API. Returns None on 404."""
        url = f"{GRAPH_BASE}/users/{user_id}/manager"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 404:
                return None
            self._raise_for_status(resp)
            return resp.json()

    async def get_user_photo(self, user_id: str) -> bytes | None:
        """Fetch user's profile photo bytes. Returns None on 404."""
        url = f"{GRAPH_BASE}/users/{user_id}/photo/$value"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 404:
                return None
            self._raise_for_status(resp)
            return resp.content
