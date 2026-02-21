"""Azure AD authentication using MSAL client credentials flow."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Union

import msal
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

logger = logging.getLogger(__name__)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class GraphAuth:
    """Acquires and caches Microsoft Graph API access tokens.

    Uses MSAL's ``ConfidentialClientApplication`` which handles token
    caching and automatic refresh internally.  Supports both client-secret
    and PFX certificate credentials.
    """

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
        """Return the MSAL client_credential value.

        If a certificate path is configured, load the PFX and return a dict
        with ``private_key`` and ``thumbprint``.  Otherwise fall back to the
        plain client secret string.
        """
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
        """Return a valid access token, refreshing silently if needed.

        Raises ``RuntimeError`` if a token cannot be obtained.
        """
        app = self._get_app()

        # Try the cache first (MSAL handles expiry checks)
        result = app.acquire_token_silent(GRAPH_SCOPE, account=None)
        if result and "access_token" in result:
            return result["access_token"]

        # Cache miss — acquire a new token
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if result and "access_token" in result:
            logger.debug("Acquired new Graph API access token")
            return result["access_token"]

        error = result.get("error_description", result.get("error", "unknown error"))
        logger.error("Failed to acquire Graph API token: %s", error)
        raise RuntimeError(f"Graph API token acquisition failed: {error}")
