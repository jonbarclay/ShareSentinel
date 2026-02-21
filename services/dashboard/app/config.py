"""Dashboard configuration from environment variables."""

import os


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://sharesentinel:devpassword123@localhost:5432/sharesentinel",
)
