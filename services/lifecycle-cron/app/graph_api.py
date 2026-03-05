"""Graph API authentication and permission removal.

Re-exports ``GraphAuth`` from the shared ``sharesentinel_common`` package.
Falls back to a local implementation if the shared package is not installed.
"""

from __future__ import annotations

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
