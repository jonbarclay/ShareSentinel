"""Graph API delegated token management for content inspection."""

import logging
import time
from typing import Optional

import httpx
from fastapi import Request

from . import config
from .auth import _get_session, _set_session, _get_current_session_id

logger = logging.getLogger(__name__)

_TOKEN_ENDPOINT = (
    f"https://login.microsoftonline.com/{config.OIDC_TENANT_ID}/oauth2/v2.0/token"
)


async def get_graph_token(request: Request) -> Optional[str]:
    """Get a valid Graph API access token from the current session.

    Refreshes the token automatically if expired.
    Returns None if no delegated session exists.
    """
    session_id = _get_current_session_id(request)
    if not session_id:
        return None

    session = await _get_session(request, session_id)
    if not session:
        return None

    access_token = session.get("graph_access_token")
    expires_at = session.get("graph_token_expires_at", 0)
    refresh_token = session.get("graph_refresh_token")

    # If token is still valid (with 5-minute buffer), return it
    if access_token and time.time() < (expires_at - 300):
        return access_token

    # Try to refresh
    if not refresh_token:
        logger.warning("No refresh token available for session %s", session_id[:8])
        return None

    new_tokens = await _refresh_graph_token(refresh_token)
    if not new_tokens:
        return None

    # Update session with new tokens
    session["graph_access_token"] = new_tokens["access_token"]
    session["graph_refresh_token"] = new_tokens.get("refresh_token", refresh_token)
    session["graph_token_expires_at"] = int(time.time()) + new_tokens.get("expires_in", 3600)
    await _set_session(request, session_id, session)

    return new_tokens["access_token"]


async def _refresh_graph_token(refresh_token: str) -> Optional[dict]:
    """Exchange a refresh token for a new access token."""
    data = {
        "client_id": config.OIDC_CLIENT_ID,
        "client_secret": config.OIDC_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": "openid profile email offline_access https://graph.microsoft.com/Notes.Read.All https://graph.microsoft.com/Files.Read.All",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _TOKEN_ENDPOINT,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.error("Token refresh failed: %s", resp.text[:500])
            return None
        return resp.json()
    except Exception:
        logger.exception("Token refresh request failed")
        return None
