"""Microsoft Graph API client for file metadata and downloads."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict
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
                logger.warning("Graph 400 response: %s", resp.text[:500])
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
