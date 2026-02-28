"""Dashboard configuration from environment variables."""

import logging
import os
import sys

logger = logging.getLogger(__name__)


# --- Database ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    logger.critical("DATABASE_URL environment variable is required but not set")
    sys.exit(1)

ALLOWED_ORIGINS = [
    origin.strip() for origin in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:8080"
    ).split(",") if origin.strip()
]

# --- Entra ID / OpenID Connect ---
OIDC_TENANT_ID = os.environ.get("OIDC_TENANT_ID", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get(
    "OIDC_REDIRECT_URI",
    "https://sharesentinel.uvu.edu/api/auth/callback",
)
OIDC_AUTHORITY = f"https://login.microsoftonline.com/{OIDC_TENANT_ID}/v2.0"

OIDC_ALLOWED_GROUP_IDS: list[str] = [
    gid.strip() for gid in os.environ.get("OIDC_ALLOWED_GROUP_IDS", "").split(",")
    if gid.strip()
]

# --- Role-based access control ---
OIDC_ADMIN_GROUP_IDS: list[str] = [
    gid.strip() for gid in os.environ.get("OIDC_ADMIN_GROUP_IDS", "").split(",")
    if gid.strip()
]
OIDC_ANALYST_GROUP_IDS: list[str] = [
    gid.strip() for gid in os.environ.get("OIDC_ANALYST_GROUP_IDS", "").split(",")
    if gid.strip()
]

# --- Session ---
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "28800"))  # 8 hours

# --- Redis ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
SESSION_REDIS_DB = int(os.environ.get("SESSION_REDIS_DB", "1"))

# --- Microsoft Graph API (for SharePoint site search) ---
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CERTIFICATE = os.environ.get("AZURE_CERTIFICATE", "")
AZURE_CERTIFICATE_PASS = os.environ.get("AZURE_CERTIFICATE_PASS", "")

# --- Dashboard URL (used for logout redirect, etc.) ---
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://sharesentinel.uvu.edu")

# --- Auth feature toggle ---
# Auth is enabled by default. Set AUTH_DISABLED=true to explicitly disable.
_auth_disabled_explicit = os.environ.get("AUTH_DISABLED", "").lower() == "true"

if _auth_disabled_explicit:
    AUTH_ENABLED = False
    logger.warning(
        "*** AUTHENTICATION IS DISABLED (AUTH_DISABLED=true) — "
        "all endpoints are publicly accessible ***"
    )
elif OIDC_CLIENT_ID:
    AUTH_ENABLED = True
    # Validate session secret when auth is enabled
    if not SESSION_SECRET_KEY:
        logger.critical(
            "SESSION_SECRET_KEY environment variable is required when "
            "authentication is enabled. Set a strong random secret."
        )
        sys.exit(1)
else:
    logger.critical(
        "OIDC_CLIENT_ID is not configured and AUTH_DISABLED is not set to 'true'. "
        "Either configure OIDC for authentication or explicitly set "
        "AUTH_DISABLED=true to run without authentication."
    )
    sys.exit(1)

# --- Browser Auth (SharePoint root for cookie capture) ---
SHAREPOINT_ROOT_URL = os.environ.get("SHAREPOINT_ROOT_URL", "https://www.office.com")
