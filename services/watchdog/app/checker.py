"""Per-loop health check logic for lifecycle-cron heartbeats."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import asyncpg
import redis.asyncio as aioredis

from .config import WatchdogConfig

logger = logging.getLogger(__name__)

HEARTBEAT_KEYS = {
    "audit_poller": "sharesentinel:lifecycle:heartbeat:audit_poller",
    "lifecycle": "sharesentinel:lifecycle:heartbeat:lifecycle",
    "site_policy": "sharesentinel:lifecycle:heartbeat:site_policy",
    "folder_rescan": "sharesentinel:lifecycle:heartbeat:folder_rescan",
}


@dataclass
class LoopHealth:
    name: str
    is_healthy: bool
    heartbeat_age_seconds: float | None  # None if key missing
    failure_type: str | None  # 'dead', 'stale', None
    last_error: str | None  # audit_poll_state.error_message (audit_poller only)


async def check_all_loops(
    redis_client: aioredis.Redis,
    db_pool: asyncpg.Pool,
    config: WatchdogConfig,
) -> list[LoopHealth]:
    """Check health of all 4 lifecycle-cron loops.

    Returns a LoopHealth for each loop that has a heartbeat key registered.
    Loops that are disabled won't have heartbeat keys and are omitted.
    """
    now = time.time()
    results: list[LoopHealth] = []

    for loop_name, redis_key in HEARTBEAT_KEYS.items():
        raw = await redis_client.get(redis_key)

        if raw is None:
            # Key missing — loop either never started or container is down
            results.append(LoopHealth(
                name=loop_name,
                is_healthy=False,
                heartbeat_age_seconds=None,
                failure_type="dead",
                last_error=None,
            ))
            continue

        try:
            ts = float(raw)
        except (ValueError, TypeError):
            results.append(LoopHealth(
                name=loop_name,
                is_healthy=False,
                heartbeat_age_seconds=None,
                failure_type="dead",
                last_error=f"Invalid heartbeat value: {raw!r}",
            ))
            continue

        age = now - ts
        is_stale = age > config.heartbeat_stale_seconds

        results.append(LoopHealth(
            name=loop_name,
            is_healthy=not is_stale,
            heartbeat_age_seconds=round(age, 1),
            failure_type="dead" if is_stale else None,
            last_error=None,
        ))

    # Additional check for audit_poller: alive but failing (poll state stale)
    audit_health = next((h for h in results if h.name == "audit_poller"), None)
    if audit_health and audit_health.is_healthy:
        poll_info = await _check_audit_poll_state(db_pool, config)
        if poll_info:
            audit_health.failure_type = "stale"
            audit_health.is_healthy = False
            audit_health.last_error = poll_info

    return results


async def _check_audit_poll_state(
    db_pool: asyncpg.Pool,
    config: WatchdogConfig,
) -> str | None:
    """Check audit_poll_state for alive-but-failing condition.

    Returns error message string if poll state indicates failure, None if OK.
    """
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_poll_status, error_message, updated_at
                FROM audit_poll_state
                WHERE id = 1
                """
            )
    except Exception:
        logger.warning("Failed to query audit_poll_state", exc_info=True)
        return None

    if not row:
        return None

    updated_at = row["updated_at"]
    if updated_at is None:
        return None

    age_seconds = (
        time.time() - updated_at.timestamp()
    )

    if (
        row["last_poll_status"] == "error"
        and age_seconds < config.poll_stale_seconds
    ):
        # Recent error but within threshold — report but not stale yet
        return None

    if age_seconds > config.poll_stale_seconds:
        error_msg = row.get("error_message") or "unknown error"
        return (
            f"Last successful poll: {int(age_seconds // 60)} min ago. "
            f"Last error: {error_msg}"
        )

    return None
