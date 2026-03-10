"""Watchdog service — monitors lifecycle-cron loops, auto-restarts, and alerts via Teams."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import asyncpg
import redis.asyncio as aioredis

from .checker import LoopHealth, check_all_loops
from .config import WatchdogConfig
from .notifier import TeamsNotifier
from .remediator import ContainerRemediator

logger = logging.getLogger(__name__)


def _redact_url(url: str) -> str:
    """Redact password from a connection URL for safe logging."""
    parsed = urlparse(url)
    if parsed.password:
        return url.replace(f":{parsed.password}@", ":***@")
    return url

# Redis state keys
KEY_CONSECUTIVE_FAILURES = "sharesentinel:watchdog:consecutive_failures"
KEY_LAST_ALERT_TS = "sharesentinel:watchdog:last_alert_ts"
KEY_LAST_RESTART_TS = "sharesentinel:watchdog:last_restart_ts"
KEY_RESTART_COUNT = "sharesentinel:watchdog:restart_count"
KEY_PREV_UNHEALTHY = "sharesentinel:watchdog:prev_unhealthy"

TTL_2H = 7200
TTL_1H = 3600


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_loop_status(results: list[LoopHealth]) -> str:
    lines = []
    for h in results:
        if h.is_healthy:
            status = "OK"
        elif h.failure_type == "dead":
            age = f"{int(h.heartbeat_age_seconds)} sec" if h.heartbeat_age_seconds is not None else "no heartbeat"
            status = f"DOWN - {age}"
        elif h.failure_type == "stale":
            status = f"STALE - {h.last_error}" if h.last_error else "STALE"
        else:
            status = "UNKNOWN"
        lines.append(f"  {h.name}: {status}")
    return "\n".join(lines)


async def _record_alert(
    db_pool: asyncpg.Pool,
    alert_type: str,
    severity: str,
    loop_name: str | None,
    message: str,
    details: dict | None = None,
    remediation_action: str | None = None,
    remediation_success: bool | None = None,
) -> None:
    """Insert a row into the watchdog_alerts table."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO watchdog_alerts
                    (alert_type, severity, loop_name, message, details,
                     remediation_action, remediation_success)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                """,
                alert_type,
                severity,
                loop_name,
                message,
                json.dumps(details or {}),
                remediation_action,
                remediation_success,
            )
    except Exception:
        logger.warning("Failed to record watchdog alert in DB", exc_info=True)


async def _can_send_alert(
    redis_client: aioredis.Redis, config: WatchdogConfig
) -> bool:
    """Check alert dedup cooldown."""
    raw = await redis_client.get(KEY_LAST_ALERT_TS)
    if raw is None:
        return True
    try:
        last_ts = float(raw)
        return (time.time() - last_ts) >= config.alert_cooldown_seconds
    except (ValueError, TypeError):
        return True


async def _mark_alert_sent(redis_client: aioredis.Redis) -> None:
    await redis_client.set(KEY_LAST_ALERT_TS, str(time.time()), ex=TTL_2H)


async def _get_restart_count(redis_client: aioredis.Redis) -> int:
    raw = await redis_client.get(KEY_RESTART_COUNT)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


async def _increment_restart_count(redis_client: aioredis.Redis) -> int:
    count = await redis_client.incr(KEY_RESTART_COUNT)
    await redis_client.expire(KEY_RESTART_COUNT, TTL_1H)
    await redis_client.set(KEY_LAST_RESTART_TS, str(time.time()), ex=TTL_1H)
    return count


async def _get_consecutive_failures(redis_client: aioredis.Redis) -> int:
    raw = await redis_client.get(KEY_CONSECUTIVE_FAILURES)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


async def _set_consecutive_failures(redis_client: aioredis.Redis, count: int) -> None:
    await redis_client.set(KEY_CONSECUTIVE_FAILURES, str(count), ex=TTL_2H)


async def _was_previously_unhealthy(redis_client: aioredis.Redis) -> bool:
    raw = await redis_client.get(KEY_PREV_UNHEALTHY)
    return raw == "1"


async def _set_prev_unhealthy(redis_client: aioredis.Redis, unhealthy: bool) -> None:
    await redis_client.set(KEY_PREV_UNHEALTHY, "1" if unhealthy else "0", ex=TTL_2H)


async def check_and_remediate(
    redis_client: aioredis.Redis,
    db_pool: asyncpg.Pool,
    config: WatchdogConfig,
    notifier: TeamsNotifier,
    remediator: ContainerRemediator,
) -> None:
    """Run one check cycle: inspect all loops, alert and remediate as needed."""
    results = await check_all_loops(redis_client, db_pool, config)

    if not results:
        logger.debug("No heartbeat keys found — lifecycle-cron may not be running yet")
        return

    all_healthy = all(h.is_healthy for h in results)
    dead_loops = [h for h in results if h.failure_type == "dead"]
    stale_loops = [h for h in results if h.failure_type == "stale"]
    was_unhealthy = await _was_previously_unhealthy(redis_client)

    # --- All healthy ---
    if all_healthy:
        if was_unhealthy:
            # Recovery detected
            consecutive = await _get_consecutive_failures(redis_client)
            downtime_min = consecutive * (config.check_interval_seconds // 60)
            msg = (
                f"RESOLVED: ShareSentinel lifecycle-cron recovered\n\n"
                f"All loops healthy. Estimated downtime: ~{downtime_min} min.\n"
                f"Recovery method: auto-restart\n"
                f"Time: {_now_str()}"
            )
            logger.info("All loops recovered")

            await _record_alert(
                db_pool, "loop_recovered", "info", None, msg,
                remediation_action="none",
            )

            if notifier.is_configured and await _can_send_alert(redis_client, config):
                await notifier.send_alert(msg)
                await _mark_alert_sent(redis_client)

            await _set_consecutive_failures(redis_client, 0)
            await _set_prev_unhealthy(redis_client, False)
        else:
            logger.debug("All loops healthy")
        return

    # --- Unhealthy ---
    await _set_prev_unhealthy(redis_client, True)
    consecutive = await _get_consecutive_failures(redis_client)
    consecutive += 1
    await _set_consecutive_failures(redis_client, consecutive)

    container_status = await remediator.get_container_status(
        config.lifecycle_container_name
    )

    # Determine action
    action_taken = "none"
    restart_success: bool | None = None

    if dead_loops:
        # At least one loop has a stale/missing heartbeat — restart
        restart_count = await _get_restart_count(redis_client)

        if restart_count >= config.max_restarts_per_hour:
            # Restart cap reached — escalate
            severity = "critical"
            action_taken = "escalation"
            msg = (
                f"CRITICAL: ShareSentinel lifecycle-cron unrecoverable\n\n"
                f"Loop status:\n{_format_loop_status(results)}\n\n"
                f"Container status: {container_status or 'unknown'}\n"
                f"Restart attempts this hour: {restart_count} (limit: {config.max_restarts_per_hour})\n"
                f"Action: STOPPED auto-restarting. Manual intervention required.\n"
                f"Time: {_now_str()}"
            )
            logger.error("Restart cap reached (%d/%d), escalating", restart_count, config.max_restarts_per_hour)
        elif config.auto_restart:
            severity = "warning"
            action_taken = "container_restart"
            restart_success = await remediator.restart_container(
                config.lifecycle_container_name
            )
            new_count = await _increment_restart_count(redis_client)

            msg = (
                f"ALERT: ShareSentinel lifecycle-cron loop(s) unhealthy\n\n"
                f"Loop status:\n{_format_loop_status(results)}\n\n"
                f"Container status: {container_status or 'unknown'}\n"
                f"Action taken: {'Restarted' if restart_success else 'Restart FAILED for'} "
                f"{config.lifecycle_container_name} "
                f"(attempt {new_count}/{config.max_restarts_per_hour} this hour)\n"
                f"Time: {_now_str()}"
            )
            if restart_success:
                logger.info("Container restarted (attempt %d)", new_count)
            else:
                logger.error("Container restart failed (attempt %d)", new_count)
        else:
            severity = "warning"
            msg = (
                f"ALERT: ShareSentinel lifecycle-cron loop(s) unhealthy\n\n"
                f"Loop status:\n{_format_loop_status(results)}\n\n"
                f"Container status: {container_status or 'unknown'}\n"
                f"Auto-restart disabled. Manual intervention required.\n"
                f"Time: {_now_str()}"
            )
            logger.warning("Dead loops detected, auto-restart disabled")

        # Record in DB
        for h in dead_loops:
            await _record_alert(
                db_pool, "loop_dead", severity, h.name, msg,
                details={"heartbeat_age": h.heartbeat_age_seconds, "consecutive": consecutive},
                remediation_action=action_taken,
                remediation_success=restart_success,
            )

    elif stale_loops:
        # Only stale (alive but failing) — don't restart immediately
        if consecutive >= config.stale_checks_before_restart and config.auto_restart:
            # Persistent stale — restart
            restart_count = await _get_restart_count(redis_client)
            if restart_count < config.max_restarts_per_hour:
                action_taken = "container_restart"
                restart_success = await remediator.restart_container(
                    config.lifecycle_container_name
                )
                new_count = await _increment_restart_count(redis_client)
                severity = "warning"
                msg = (
                    f"ALERT: ShareSentinel loop(s) persistently failing\n\n"
                    f"Loop status:\n{_format_loop_status(results)}\n\n"
                    f"Consecutive failures: {consecutive}\n"
                    f"Action taken: {'Restarted' if restart_success else 'Restart FAILED for'} "
                    f"{config.lifecycle_container_name}\n"
                    f"Time: {_now_str()}"
                )
            else:
                severity = "critical"
                action_taken = "escalation"
                msg = (
                    f"CRITICAL: ShareSentinel lifecycle-cron unrecoverable\n\n"
                    f"Loop status:\n{_format_loop_status(results)}\n\n"
                    f"Restart cap reached ({restart_count}/{config.max_restarts_per_hour}).\n"
                    f"Manual intervention required.\n"
                    f"Time: {_now_str()}"
                )
        else:
            severity = "warning"
            stale_info = stale_loops[0]
            msg = (
                f"WARNING: ShareSentinel audit poller failing\n\n"
                f"Loop status:\n{_format_loop_status(results)}\n\n"
                f"No restart needed — loop is alive but calls are failing.\n"
                f"Consecutive checks: {consecutive}/{config.stale_checks_before_restart} before restart\n"
                f"Time: {_now_str()}"
            )
            logger.warning("Stale loop(s) detected (check %d/%d)", consecutive, config.stale_checks_before_restart)

        for h in stale_loops:
            await _record_alert(
                db_pool, "loop_stale", severity, h.name, msg,
                details={"last_error": h.last_error, "consecutive": consecutive},
                remediation_action=action_taken,
                remediation_success=restart_success,
            )

    # Send Teams alert (with dedup)
    if notifier.is_configured and await _can_send_alert(redis_client, config):
        sent = await notifier.send_alert(msg)
        if sent:
            await _mark_alert_sent(redis_client)


async def main() -> None:
    config = WatchdogConfig.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logger.info(
        "Watchdog starting (interval=%ds, stale=%ds, restart=%s, container=%s)",
        config.check_interval_seconds,
        config.heartbeat_stale_seconds,
        config.auto_restart,
        config.lifecycle_container_name,
    )

    # Connect to Redis
    redis_client = aioredis.from_url(config.redis_url, decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception:
        logger.critical("Cannot reach Redis at %s", _redact_url(config.redis_url), exc_info=True)
        raise

    # Connect to PostgreSQL
    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=2)
    logger.info("Database pool created")

    notifier = TeamsNotifier(config.teams_webhook_url)
    if notifier.is_configured:
        logger.info("Teams webhook configured")
    else:
        logger.warning("TEAMS_WEBHOOK_URL not set — alerts will be logged only")

    remediator = ContainerRemediator()

    logger.info("Watchdog running — checking every %ds", config.check_interval_seconds)

    try:
        while True:
            try:
                await check_and_remediate(
                    redis_client, db_pool, config, notifier, remediator,
                )
            except Exception:
                logger.exception("Error in watchdog check cycle")
            await asyncio.sleep(config.check_interval_seconds)
    finally:
        await redis_client.aclose()
        await db_pool.close()
        logger.info("Watchdog shut down")


if __name__ == "__main__":
    asyncio.run(main())
