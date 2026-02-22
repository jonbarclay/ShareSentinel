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
