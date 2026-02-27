"""Dashboard configuration from environment variables."""

import os


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://sharesentinel:devpassword123@localhost:5432/sharesentinel",
)

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

# --- Session ---
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "change-me-in-production")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "28800"))  # 8 hours

# --- Redis ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
SESSION_REDIS_DB = int(os.environ.get("SESSION_REDIS_DB", "1"))

# --- Auth feature toggle ---
# SSO is enabled when OIDC_CLIENT_ID is configured
AUTH_ENABLED = bool(OIDC_CLIENT_ID)
