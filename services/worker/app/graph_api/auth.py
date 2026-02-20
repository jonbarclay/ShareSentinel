"""Azure AD authentication using MSAL client credentials flow."""

from __future__ import annotations

import logging
from typing import Optional

import msal

logger = logging.getLogger(__name__)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class GraphAuth:
    """Acquires and caches Microsoft Graph API access tokens.

    Uses MSAL's ``ConfidentialClientApplication`` which handles token
    caching and automatic refresh internally.
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._app: Optional[msal.ConfidentialClientApplication] = None
        self._client_id = client_id
        self._client_secret = client_secret

    def _get_app(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                client_id=self._client_id,
                client_credential=self._client_secret,
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
