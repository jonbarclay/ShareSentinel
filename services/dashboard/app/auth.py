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
import time
import urllib.parse
from typing import Optional

import httpx
from authlib.jose import jwt, JsonWebKey
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

from . import config

# JWKS and OIDC config cache TTL (seconds) — refresh after 24 hours
_CACHE_TTL = 86400

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


def _validate_claims(claims: dict) -> None:
    """Validate aud and iss claims to prevent cross-app token abuse.

    Raises ``ValueError`` if any claim is invalid.
    """
    # Validate audience
    aud = claims.get("aud")
    if isinstance(aud, list):
        if config.OIDC_CLIENT_ID not in aud:
            raise ValueError(f"Token audience {aud} does not include {config.OIDC_CLIENT_ID}")
    elif aud != config.OIDC_CLIENT_ID:
        raise ValueError(f"Token audience '{aud}' does not match '{config.OIDC_CLIENT_ID}'")

    # Validate issuer
    expected_iss = f"https://login.microsoftonline.com/{config.OIDC_TENANT_ID}/v2.0"
    iss = claims.get("iss")
    if iss != expected_iss:
        raise ValueError(f"Token issuer '{iss}' does not match '{expected_iss}'")


async def _get_oidc_config(request: Request) -> dict:
    """Fetch and cache the OIDC discovery document (TTL-based)."""
    cache = request.app.state.oidc_config_cache
    if cache and (time.monotonic() - cache["_fetched_at"]) < _CACHE_TTL:
        return cache
    async with httpx.AsyncClient() as client:
        resp = await client.get(_discovery_url())
        resp.raise_for_status()
        data = resp.json()
        data["_fetched_at"] = time.monotonic()
        request.app.state.oidc_config_cache = data
        return data


async def _get_jwks(request: Request, force_refresh: bool = False) -> dict:
    """Fetch and cache the JWKS from the OIDC provider (TTL-based)."""
    cache = request.app.state.jwks_cache
    if (
        not force_refresh
        and cache
        and (time.monotonic() - cache.get("_fetched_at", 0)) < _CACHE_TTL
    ):
        return cache
    oidc_cfg = await _get_oidc_config(request)
    async with httpx.AsyncClient() as client:
        resp = await client.get(oidc_cfg["jwks_uri"])
        resp.raise_for_status()
        data = resp.json()
        data["_fetched_at"] = time.monotonic()
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


def _user_roles(user: dict) -> set[str]:
    """Determine the set of roles for a user based on group membership.

    Roles:
    - ``viewer``: any authenticated user
    - ``analyst``: user in analyst OR admin groups
    - ``admin``: user in admin groups only

    When neither admin nor analyst group IDs are configured, all
    authenticated users get all roles (graceful degradation).
    """
    roles = {"viewer"}
    user_groups = set(user.get("groups", []))

    admin_groups = set(config.OIDC_ADMIN_GROUP_IDS)
    analyst_groups = set(config.OIDC_ANALYST_GROUP_IDS)

    # Graceful degradation: no RBAC groups configured = everyone gets all roles
    if not admin_groups and not analyst_groups:
        return {"viewer", "analyst", "admin"}

    if admin_groups and user_groups.intersection(admin_groups):
        roles.update({"analyst", "admin"})
    if analyst_groups and user_groups.intersection(analyst_groups):
        roles.add("analyst")

    return roles


def require_role(*roles: str):
    """Return a FastAPI dependency that enforces role-based access.

    Usage::

        @router.patch("/verdicts/{event_id}")
        async def review(request: Request, user=Depends(require_role("analyst"))):
            ...

    When auth is disabled (AUTH_ENABLED=False), all role checks pass.
    """
    from fastapi import Depends, HTTPException

    async def _check(request: Request):
        if not config.AUTH_ENABLED:
            return {"email": "anonymous", "name": "Anonymous", "groups": []}
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user_roles = _user_roles(user)
        if not user_roles.intersection(set(roles)):
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions",
            )
        return user

    return Depends(_check)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/login")
async def login(request: Request):
    """Redirect user to Entra ID for authentication."""
    oidc_cfg = await _get_oidc_config(request)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    # Store state and nonce in Redis briefly for CSRF and replay validation
    redis = request.app.state.session_redis
    await redis.set(f"ss:oauth_state:{state}", nonce, ex=600)  # 10 min

    params = {
        "client_id": config.OIDC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.OIDC_REDIRECT_URI,
        "response_mode": "query",
        "scope": "openid profile email offline_access https://graph.microsoft.com/Notes.Read.All https://graph.microsoft.com/Files.Read.All",
        "state": state,
        "nonce": nonce,
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

    # Validate state (CSRF) and retrieve expected nonce
    redis = request.app.state.session_redis
    state_key = f"ss:oauth_state:{state}"
    expected_nonce = await redis.get(state_key)
    if not expected_nonce:
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
        "scope": "openid profile email offline_access https://graph.microsoft.com/Notes.Read.All https://graph.microsoft.com/Files.Read.All",
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

    # Validate the ID token (retry once with refreshed JWKS on failure,
    # handles key rotation)
    try:
        jwks = await _get_jwks(request)
        claims = jwt.decode(
            id_token,
            JsonWebKey.import_key_set(jwks),
        )
        claims.validate()
        _validate_claims(claims)
    except Exception:
        try:
            logger.info("Token validation failed, refreshing JWKS and retrying")
            jwks = await _get_jwks(request, force_refresh=True)
            claims = jwt.decode(
                id_token,
                JsonWebKey.import_key_set(jwks),
            )
            claims.validate()
            _validate_claims(claims)
        except Exception as e:
            logger.error("ID token validation failed after JWKS refresh: %s", e)
            return JSONResponse({"error": "invalid_token"}, status_code=403)

    # Validate nonce to prevent token replay attacks
    token_nonce = claims.get("nonce")
    if token_nonce != expected_nonce:
        logger.error("Nonce mismatch: expected=%s, got=%s", expected_nonce, token_nonce)
        return JSONResponse({"error": "invalid_nonce"}, status_code=403)

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
        "graph_access_token": access_token,
        "graph_refresh_token": tokens.get("refresh_token"),
        "graph_token_expires_at": int(time.time()) + tokens.get("expires_in", 3600),
    }
    await _set_session(request, session_id, session_data)

    # Upsert dashboard_users (fire-and-forget — never block login)
    try:
        computed_roles = sorted(_user_roles(session_data))
        db_pool = request.app.state.db
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO dashboard_users (oid, email, display_name, groups, roles, last_seen_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, NOW())
                    ON CONFLICT (oid) DO UPDATE SET
                        email = EXCLUDED.email,
                        display_name = EXCLUDED.display_name,
                        groups = EXCLUDED.groups,
                        roles = EXCLUDED.roles,
                        last_seen_at = NOW()
                    """,
                    user_oid,
                    user_email,
                    user_name,
                    session_data.get("groups", []),
                    computed_roles,
                )
    except Exception:
        logger.warning("Failed to upsert dashboard_users", exc_info=True)

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
    """Check which allowed groups the user belongs to via Graph API.

    Uses the checkMemberGroups endpoint which handles transitive membership
    (nested groups) and avoids pagination issues with /me/memberOf.
    """
    if not config.OIDC_ALLOWED_GROUP_IDS:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://graph.microsoft.com/v1.0/me/checkMemberGroups",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"groupIds": config.OIDC_ALLOWED_GROUP_IDS},
            )
            if resp.status_code == 200:
                return resp.json().get("value", [])
            logger.error("checkMemberGroups returned %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Failed to check group membership via Graph API: %s", e)
    return []


@router.get("/me")
async def me(request: Request):
    """Return the current user's session info."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    # Strip internal token fields — never expose tokens to the browser
    safe = {k: v for k, v in user.items() if not k.startswith("graph_")}
    safe["roles"] = sorted(_user_roles(user))
    return safe


@router.get("/graph-status")
async def graph_status(request: Request):
    """Check if the current user has a valid Graph API delegated token."""
    from .auth_graph import get_graph_token
    token = await get_graph_token(request)
    return {"has_graph_token": token is not None}


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
        post_logout_uri = config.DASHBOARD_URL.rstrip("/") + "/"
        logout_url = (
            f"https://login.microsoftonline.com/{config.OIDC_TENANT_ID}"
            f"/oauth2/v2.0/logout?post_logout_redirect_uri="
            f"{urllib.parse.quote(post_logout_uri)}"
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
