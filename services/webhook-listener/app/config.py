"""Configuration loading from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Webhook listener configuration."""

    webhook_port: int = 8000
    webhook_auth_secret: str | None = None
    redis_url: str = "redis://redis:6379/0"
    dedup_ttl_seconds: int = 86400
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables."""
        return cls(
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8000")),
            webhook_auth_secret=os.getenv("WEBHOOK_AUTH_SECRET") or None,
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            dedup_ttl_seconds=int(os.getenv("DEDUP_TTL_SECONDS", "86400")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def auth_enabled(self) -> bool:
        return self.webhook_auth_secret is not None


settings = Settings.from_env()
