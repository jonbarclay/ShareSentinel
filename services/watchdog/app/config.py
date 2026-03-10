"""Configuration for the watchdog service."""

import os
from dataclasses import dataclass


@dataclass
class WatchdogConfig:
    # Connections
    database_url: str = ""
    redis_url: str = ""

    # Teams webhook (Power Automate workflow trigger)
    teams_webhook_url: str = ""

    # Check interval (how often the watchdog runs its check loop)
    check_interval_seconds: int = 120

    # Heartbeat staleness threshold (per-loop heartbeat)
    heartbeat_stale_seconds: int = 300  # 5 minutes

    # Audit poll staleness threshold (audit_poll_state.updated_at)
    poll_stale_seconds: int = 3600  # 1 hour

    # Auto-restart
    auto_restart: bool = True
    lifecycle_container_name: str = "sharesentinel-lifecycle-cron"

    # Restart limits
    max_restarts_per_hour: int = 3

    # Alert dedup cooldown
    alert_cooldown_seconds: int = 1800  # 30 minutes

    # Consecutive stale checks before restart (for alive-but-failing)
    stale_checks_before_restart: int = 3

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            redis_url=os.environ.get("REDIS_URL", ""),
            teams_webhook_url=os.environ.get("TEAMS_WEBHOOK_URL", ""),
            check_interval_seconds=int(
                os.environ.get("WATCHDOG_CHECK_INTERVAL_SECONDS", "120")
            ),
            heartbeat_stale_seconds=int(
                os.environ.get("WATCHDOG_HEARTBEAT_STALE_SECONDS", "300")
            ),
            poll_stale_seconds=int(
                os.environ.get("WATCHDOG_POLL_STALE_SECONDS", "3600")
            ),
            auto_restart=os.environ.get(
                "WATCHDOG_AUTO_RESTART", "true"
            ).lower() == "true",
            lifecycle_container_name=os.environ.get(
                "LIFECYCLE_CONTAINER_NAME", "sharesentinel-lifecycle-cron"
            ),
            max_restarts_per_hour=int(
                os.environ.get("WATCHDOG_MAX_RESTARTS_PER_HOUR", "3")
            ),
            alert_cooldown_seconds=int(
                os.environ.get("WATCHDOG_ALERT_COOLDOWN_SECONDS", "1800")
            ),
            stale_checks_before_restart=int(
                os.environ.get("WATCHDOG_STALE_CHECKS_BEFORE_RESTART", "3")
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
