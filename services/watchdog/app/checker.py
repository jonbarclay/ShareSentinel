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

# Optional loops: alert on a STALE heartbeat, but a missing key is fine
# (the loop may be disabled by config).
OPTIONAL_HEARTBEAT_KEYS = {
    "cws_verification": "sharesentinel:lifecycle:heartbeat:cws_verification",
    "cws_drift_remediation": "sharesentinel:lifecycle:heartbeat:cws_drift_remediation",
    "cws_bulk_remediation": "sharesentinel:lifecycle:heartbeat:cws_bulk_remediation",
    "cws_exception_sync": "sharesentinel:lifecycle:heartbeat:cws_exception_sync",
    "cws_allowlist_action": "sharesentinel:lifecycle:heartbeat:cws_allowlist_action",
    "cws_failure_diagnostic": "sharesentinel:lifecycle:heartbeat:cws_failure_diagnostic",
}

WORKER_HEARTBEAT_KEY = "sharesentinel:worker:heartbeat"
JOBS_QUEUE_KEY = "sharesentinel:jobs"
DLQ_KEYS = (
    "sharesentinel:site_policy_action:dlq",
    "sharesentinel:company_wide_sharing_action:dlq",
)


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


@dataclass
class SystemAlert:
    """Alert-only finding (no container restart) from the system-level sweep."""

    name: str
    severity: str  # 'warning' | 'critical'
    message: str


async def check_system_health(
    redis_client: aioredis.Redis,
    db_pool: asyncpg.Pool,
    config: WatchdogConfig,
) -> list[SystemAlert]:
    """Alert-only checks beyond the lifecycle-cron loop heartbeats.

    Covers the worker (the component doing all analysis, previously entirely
    unmonitored), queue/DLQ depths, optional CWS loop heartbeats, failed
    policy scans (incl. enumeration-floor failures), failed-event volume, and
    inspection-queue age.
    """
    now = time.time()
    alerts: list[SystemAlert] = []

    # 1. Worker heartbeat (dead worker = nothing gets analyzed)
    try:
        raw = await redis_client.get(WORKER_HEARTBEAT_KEY)
        if raw is None:
            alerts.append(SystemAlert(
                "worker", "critical",
                "Worker heartbeat key missing — worker container down or never started",
            ))
        else:
            age = now - float(raw)
            if age > config.heartbeat_stale_seconds:
                alerts.append(SystemAlert(
                    "worker", "critical",
                    f"Worker heartbeat stale ({int(age)}s old) — analysis pipeline stalled",
                ))
    except Exception:
        logger.warning("Worker heartbeat check failed", exc_info=True)

    # 2. Optional CWS loop heartbeats (stale only; missing = disabled, fine)
    for name, key in OPTIONAL_HEARTBEAT_KEYS.items():
        try:
            raw = await redis_client.get(key)
            if raw is None:
                continue
            age = now - float(raw)
            if age > config.heartbeat_stale_seconds:
                alerts.append(SystemAlert(
                    name, "warning",
                    f"{name} heartbeat stale ({int(age)}s old)",
                ))
        except Exception:
            logger.warning("Heartbeat check failed for %s", name, exc_info=True)

    # 3. Queue depth (worker falling behind or wedged)
    try:
        depth = await redis_client.llen(JOBS_QUEUE_KEY)
        if depth > config.queue_depth_alert:
            alerts.append(SystemAlert(
                "jobs_queue", "warning",
                f"Jobs queue depth {depth} exceeds threshold {config.queue_depth_alert}",
            ))
    except Exception:
        logger.warning("Queue depth check failed", exc_info=True)

    # 4. Dead-letter queues (enforcement actions that failed and were parked)
    for dlq in DLQ_KEYS:
        try:
            depth = await redis_client.llen(dlq)
            if depth > 0:
                alerts.append(SystemAlert(
                    "dead_letter", "warning",
                    f"{depth} dead-lettered enforcement action(s) on {dlq} — "
                    "inspect and re-queue or discard",
                ))
        except Exception:
            logger.warning("DLQ check failed for %s", dlq, exc_info=True)

    # 5. Failed site policy scans in the last 24h (incl. enumeration floor)
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n, MAX(error_message) AS sample
                FROM site_policy_scans
                WHERE status = 'failed' AND created_at > NOW() - INTERVAL '24 hours'
                """
            )
        if row and row["n"]:
            alerts.append(SystemAlert(
                "site_policy_scan", "critical",
                f"{row['n']} failed site policy scan(s) in 24h. "
                f"Latest error: {(row['sample'] or '')[:200]}",
            ))
    except Exception:
        logger.warning("Failed-scan check failed", exc_info=True)

    # 6. Failed events volume (files shared but never analyzed)
    try:
        async with db_pool.acquire() as conn:
            n = await conn.fetchval(
                """
                SELECT COUNT(*) FROM events
                WHERE status = 'failed'
                  AND updated_at > NOW() - INTERVAL '24 hours'
                """
            )
        if n and n > config.failed_events_alert_24h:
            alerts.append(SystemAlert(
                "failed_events", "warning",
                f"{n} events failed in the last 24h (threshold "
                f"{config.failed_events_alert_24h}) — files are going unanalyzed",
            ))
    except Exception:
        logger.warning("Failed-events check failed", exc_info=True)

    # 7. Inspection queue age (delegated content awaiting manual review)
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n,
                       EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) / 86400 AS oldest_days
                FROM events
                WHERE status = 'pending_manual_inspection'
                """
            )
        if row and row["n"] and (row["oldest_days"] or 0) > config.inspection_age_alert_days:
            alerts.append(SystemAlert(
                "inspection_queue", "warning",
                f"{row['n']} events pending manual inspection; oldest is "
                f"{int(row['oldest_days'])} days old "
                f"(threshold {config.inspection_age_alert_days}d)",
            ))
    except Exception:
        logger.warning("Inspection-queue check failed", exc_info=True)

    return alerts


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
