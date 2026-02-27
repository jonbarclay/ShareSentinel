"""Entra ID OpenID Connect authentication for the dashboard.

Provides:
- /api/auth/login   — Redirect to Entra ID authorization endpoint
- /api/auth/callback — Exchange code for tokens, validate, create session
- /api/auth/logout  — Clear session, redirect to Entra ID logout
- /api/auth/me      — Return current user info from session

Plus middleware that enforces authentication on all non-exempt routes.
"""

import json
import logging
import secrets
import urllib.parse
from typing import Optional

import httpx
from authlib.jose import jwt, JsonWebKey
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

from . import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Cookie name for session ID
SESSION_COOKIE = "ss_session"

# Routes exempt from authentication
EXEMPT_PREFIXES = ("/api/health", "/api/auth/")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _discovery_url() -> str:
    return f"{config.OIDC_AUTHORITY}/.well-known/openid-configuration"


async def _get_oidc_config(request: Request) -> dict:
    """Fetch and cache the OIDC discovery document."""
    cache = request.app.state.oidc_config_cache
    if cache:
        return cache
    async with httpx.AsyncClient() as client:
        resp = await client.get(_discovery_url())
        resp.raise_for_status()
        data = resp.json()
        request.app.state.oidc_config_cache = data
        return data


async def _get_jwks(request: Request) -> dict:
    """Fetch and cache the JWKS from the OIDC provider."""
    cache = request.app.state.jwks_cache
    if cache:
        return cache
    oidc_cfg = await _get_oidc_config(request)
    async with httpx.AsyncClient() as client:
        resp = await client.get(oidc_cfg["jwks_uri"])
        resp.raise_for_status()
        data = resp.json()
        request.app.state.jwks_cache = data
        return data


def _get_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SESSION_SECRET_KEY)


def _sign_session_id(session_id: str) -> str:
    return _get_signer().dumps(session_id)


def _unsign_session_id(signed: str) -> Optional[str]:
    try:
        return _get_signer().loads(signed, max_age=config.SESSION_TTL_SECONDS)
    except Exception:
        return None


async def _set_session(request: Request, session_id: str, data: dict):
    """Store session data in Redis with TTL."""
    redis = request.app.state.session_redis
    key = f"ss:session:{session_id}"
    await redis.set(key, json.dumps(data), ex=config.SESSION_TTL_SECONDS)


async def _get_session(request: Request, session_id: str) -> Optional[dict]:
    """Retrieve session data from Redis; refresh TTL on access."""
    redis = request.app.state.session_redis
    key = f"ss:session:{session_id}"
    raw = await redis.get(key)
    if raw is None:
        return None
    await redis.expire(key, config.SESSION_TTL_SECONDS)
    return json.loads(raw)


async def _delete_session(request: Request, session_id: str):
    redis = request.app.state.session_redis
    await redis.delete(f"ss:session:{session_id}")


def _get_current_session_id(request: Request) -> Optional[str]:
    """Extract and validate the session ID from the cookie."""
    signed = request.cookies.get(SESSION_COOKIE)
    if not signed:
        return None
    return _unsign_session_id(signed)


async def get_current_user(request: Request) -> Optional[dict]:
    """Get the current user from the session, or None."""
    session_id = _get_current_session_id(request)
    if not session_id:
        return None
    return await _get_session(request, session_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/login")
async def login(request: Request):
    """Redirect user to Entra ID for authentication."""
    oidc_cfg = await _get_oidc_config(request)
    state = secrets.token_urlsafe(32)

    # Store state in Redis briefly for CSRF validation
    redis = request.app.state.session_redis
    await redis.set(f"ss:oauth_state:{state}", "1", ex=600)  # 10 min

    params = {
        "client_id": config.OIDC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.OIDC_REDIRECT_URI,
        "response_mode": "query",
        "scope": "openid profile email",
        "state": state,
    }
    auth_url = f"{oidc_cfg['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback")
async def callback(request: Request):
    """Handle the OIDC callback — exchange code for tokens and create session."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        error_desc = request.query_params.get("error_description", "Unknown error")
        logger.error("OIDC error: %s — %s", error, error_desc)
        return JSONResponse(
            {"error": "authentication_failed", "detail": error_desc},
            status_code=403,
        )

    if not code or not state:
        return JSONResponse({"error": "missing_params"}, status_code=400)

    # Validate state (CSRF)
    redis = request.app.state.session_redis
    state_key = f"ss:oauth_state:{state}"
    valid_state = await redis.get(state_key)
    if not valid_state:
        return JSONResponse({"error": "invalid_state"}, status_code=403)
    await redis.delete(state_key)

    # Exchange code for tokens
    oidc_cfg = await _get_oidc_config(request)
    token_data = {
        "client_id": config.OIDC_CLIENT_ID,
        "client_secret": config.OIDC_CLIENT_SECRET,
        "code": code,
        "redirect_uri": config.OIDC_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            oidc_cfg["token_endpoint"],
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if token_resp.status_code != 200:
        logger.error("Token exchange failed: %s", token_resp.text)
        return JSONResponse({"error": "token_exchange_failed"}, status_code=403)

    tokens = token_resp.json()
    id_token = tokens.get("id_token")
    access_token = tokens.get("access_token")

    if not id_token:
        return JSONResponse({"error": "no_id_token"}, status_code=403)

    # Validate the ID token
    try:
        jwks = await _get_jwks(request)
        claims = jwt.decode(
            id_token,
            JsonWebKey.import_key_set(jwks),
        )
        claims.validate()
    except Exception as e:
        logger.error("ID token validation failed: %s", e)
        return JSONResponse({"error": "invalid_token"}, status_code=403)

    # Extract user info
    user_name = claims.get("name", "Unknown")
    user_email = claims.get("preferred_username") or claims.get("email", "")
    user_oid = claims.get("oid", "")

    # Check group membership
    user_groups = claims.get("groups", [])

    # If groups claim is missing (user in too many groups), fall back to Graph API
    if not user_groups and access_token and config.OIDC_ALLOWED_GROUP_IDS:
        user_groups = await _fetch_user_groups(access_token)

    # Enforce group restriction (if configured)
    if config.OIDC_ALLOWED_GROUP_IDS:
        allowed = set(config.OIDC_ALLOWED_GROUP_IDS)
        user_group_set = set(user_groups) if isinstance(user_groups, list) else set()
        if not user_group_set.intersection(allowed):
            logger.warning(
                "Access denied for %s (%s) — not in allowed groups",
                user_email, user_oid,
            )
            return JSONResponse(
                {"error": "access_denied", "detail": "You are not authorized to access this application."},
                status_code=403,
            )

    # Create session
    session_id = secrets.token_urlsafe(32)
    session_data = {
        "name": user_name,
        "email": user_email,
        "oid": user_oid,
        "groups": user_groups if isinstance(user_groups, list) else [],
    }
    await _set_session(request, session_id, session_data)

    # Set cookie and redirect to dashboard
    signed_cookie = _sign_session_id(session_id)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=signed_cookie,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=config.SESSION_TTL_SECONDS,
        path="/",
    )
    logger.info("Login successful: %s (%s)", user_email, user_name)
    return response


async def _fetch_user_groups(access_token: str) -> list[str]:
    """Fetch user group IDs from Microsoft Graph when the groups claim overflows."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/me/memberOf",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return [
                    entry["id"]
                    for entry in data.get("value", [])
                    if entry.get("@odata.type") == "#microsoft.graph.group"
                ]
    except Exception as e:
        logger.error("Failed to fetch user groups from Graph API: %s", e)
    return []


@router.get("/me")
async def me(request: Request):
    """Return the current user's session info."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    return user


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to Entra ID logout."""
    session_id = _get_current_session_id(request)
    if session_id:
        await _delete_session(request, session_id)

    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")

    # If OIDC is configured, redirect through Entra ID logout
    if config.OIDC_TENANT_ID:
        logout_url = (
            f"https://login.microsoftonline.com/{config.OIDC_TENANT_ID}"
            f"/oauth2/v2.0/logout?post_logout_redirect_uri="
            f"{urllib.parse.quote('https://sharesentinel.uvu.edu/')}"
        )
        response = RedirectResponse(url=logout_url, status_code=302)
        response.delete_cookie(SESSION_COOKIE, path="/")

    return response


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication on all non-exempt routes.

    - Exempt: /api/health, /api/auth/*
    - API routes (/api/*) get 401 JSON
    - Page routes get redirected to /api/auth/login
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth check for exempt routes
        for prefix in EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Check session
        user = await get_current_user(request)
        if user is not None:
            # Attach user to request state for downstream use
            request.state.user = user
            return await call_next(request)

        # Not authenticated
        if path.startswith("/api/"):
            return JSONResponse(
                {"error": "not_authenticated", "detail": "Please log in."},
                status_code=401,
            )

        # Page routes: redirect to login
        return RedirectResponse(url="/api/auth/login", status_code=302)
