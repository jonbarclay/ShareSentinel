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
