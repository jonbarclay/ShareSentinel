"""Worker entry point -- consumes jobs from the Redis queue."""

import asyncio
import json
import logging
import shutil
import signal
import time
from pathlib import Path
from typing import Any, List, Optional

import redis.asyncio as aioredis

from app.ai.base_provider import BaseAIProvider
from app.ai.anthropic_provider import AnthropicProvider
from app.ai.openai_provider import OpenAIProvider
from app.ai.gemini_provider import GeminiProvider
from app.ai.prompt_manager import PromptManager
from app.config import Config
from app.database.connection import create_pool, run_migrations
from app.logging_config import setup_logging
from app.notifications.base_notifier import BaseNotifier, NotificationDispatcher
from app.notifications.email_notifier import EmailNotifier
from app.notifications.jira_notifier import JiraNotifier
from app.pipeline.orchestrator import process_job

logger = logging.getLogger(__name__)

from app.database.repositories import AuditLogRepository, EventRepository

QUEUE_KEY = "sharesentinel:jobs"
HEARTBEAT_KEY = "sharesentinel:worker:heartbeat"
HEARTBEAT_INTERVAL_S = 60
CLEANUP_INTERVAL_S = 300  # 5 minutes
STALE_FILE_AGE_S = 1800  # 30 minutes


# -- Factory helpers ---------------------------------------------------------


def create_ai_provider(config: Config) -> BaseAIProvider:
    """Instantiate the configured AI provider."""
    prompt_manager = PromptManager(template_dir=config.prompt_template_dir)

    provider_name = config.ai_provider.lower()
    if provider_name == "anthropic":
        return AnthropicProvider(
            api_key=config.anthropic_api_key,
            model=config.anthropic_model,
            prompt_manager=prompt_manager,
            temperature=config.ai_temperature,
        )
    elif provider_name == "openai":
        return OpenAIProvider(
            api_key=config.openai_api_key,
            model=config.openai_model,
            prompt_manager=prompt_manager,
            temperature=config.ai_temperature,
        )
    elif provider_name == "gemini":
        return GeminiProvider(
            api_key=config.gemini_api_key,
            model=config.gemini_model,
            prompt_manager=prompt_manager,
            temperature=config.ai_temperature,
            project=config.vertex_project,
            location=config.vertex_location,
        )
    else:
        raise ValueError(f"Unknown AI provider: {config.ai_provider}")


def create_second_look_provider(config: Config) -> Optional[BaseAIProvider]:
    """Build the second-look AI provider, or None if disabled."""
    if not config.second_look_enabled:
        return None

    prompt_manager = PromptManager(template_dir=config.prompt_template_dir)

    if config.second_look_provider.lower() == "gemini":
        provider = GeminiProvider(
            api_key=config.gemini_api_key,
            model=config.second_look_model,
            prompt_manager=prompt_manager,
            temperature=0.0,
            project=config.vertex_project,
            location=config.vertex_location,
        )
    else:
        raise ValueError(f"Unsupported second-look provider: {config.second_look_provider}")

    logger.info("Second-look provider ready: %s (%s)", config.second_look_provider, config.second_look_model)
    return provider


def create_notification_dispatcher(config: Config) -> NotificationDispatcher:
    """Build a NotificationDispatcher with all configured channels."""
    notifiers: List[BaseNotifier] = []

    if not config.analyst_notification_enabled:
        logger.info("Analyst notifications disabled (ANALYST_NOTIFICATION_ENABLED=false)")
        return NotificationDispatcher(notifiers)

    for channel in config.notification_channels:
        channel = channel.strip().lower()
        if channel == "email":
            if config.smtp_host and config.email_to:
                notifiers.append(EmailNotifier(
                    smtp_host=config.smtp_host,
                    smtp_port=config.smtp_port,
                    smtp_user=config.smtp_user,
                    smtp_password=config.smtp_password,
                    from_address=config.email_from,
                    to_addresses=config.email_to,
                    use_tls=config.smtp_use_tls,
                    dashboard_url=config.dashboard_url,
                ))
                logger.info("Email notifier configured (to=%s)", config.email_to)
            else:
                logger.warning(
                    "Email channel requested but SMTP_HOST or EMAIL_TO not configured"
                )
        elif channel == "jira":
            if config.jira_url and config.jira_api_token:
                notifiers.append(JiraNotifier(
                    jira_url=config.jira_url,
                    jira_email=config.jira_email,
                    jira_api_token=config.jira_api_token,
                    project_key=config.jira_project_key,
                    issue_type=config.jira_issue_type,
                ))
                logger.info("Jira notifier configured (project=%s)", config.jira_project_key)
            else:
                logger.warning(
                    "Jira channel requested but JIRA_URL or JIRA_API_TOKEN not configured"
                )
        else:
            logger.warning("Unknown notification channel: %s", channel)

    if not notifiers:
        logger.warning("No notification channels configured -- alerts will be logged only")

    return NotificationDispatcher(notifiers)


# -- Background tasks --------------------------------------------------------

async def heartbeat_loop(redis_conn: aioredis.Redis) -> None:
    """Write a heartbeat timestamp to Redis every HEARTBEAT_INTERVAL_S seconds."""
    while True:
        try:
            await redis_conn.set(HEARTBEAT_KEY, str(int(time.time())))
        except Exception:
            logger.warning("Failed to write heartbeat to Redis", exc_info=True)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def cleanup_stale_files(tmpfs_path: str) -> None:
    """Periodically remove directories on tmpfs older than STALE_FILE_AGE_S."""
    base = Path(tmpfs_path)
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_S)
        try:
            if not base.exists():
                continue
            cutoff = time.time() - STALE_FILE_AGE_S
            for entry in base.iterdir():
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry)
                    logger.warning("Cleaned up stale directory: %s", entry.name)
        except Exception:
            logger.error("Error during stale file cleanup", exc_info=True)


# -- Startup recovery --------------------------------------------------------


async def recover_stuck_events(
    db_pool: Any,
    redis_conn: aioredis.Redis,
    config: Config,
) -> None:
    """Sweep for events stuck in 'processing' after a container restart.

    Events below the max retry threshold are requeued; the rest are marked failed.
    """
    event_repo = EventRepository(db_pool)
    audit_repo = AuditLogRepository(db_pool)

    stuck = await event_repo.get_stuck_processing_events(config.stuck_processing_timeout_minutes)
    if not stuck:
        logger.info("Startup recovery sweep: no stuck events found")
        return

    logger.warning("Startup recovery sweep: found %d stuck 'processing' events", len(stuck))

    requeued = 0
    failed = 0
    for row in stuck:
        eid = row["event_id"]
        current_retries = row.get("retry_count") or 0

        if current_retries >= config.max_event_retries:
            await event_repo.update_event_status(
                eid, "failed", failure_reason="stuck_processing_max_retries",
            )
            await audit_repo.log(eid, "recovery_failed", {
                "retry_count": current_retries,
                "reason": "stuck_processing_max_retries",
            }, status="error")
            failed += 1
            continue

        new_count = await event_repo.requeue_event(eid, "stuck_processing_recovery")
        await audit_repo.log(eid, "recovery_requeued", {"retry_count": new_count})

        # Reconstruct the job payload from DB columns and push to Redis
        job_payload = {
            "event_id": eid,
            "operation": row.get("operation", ""),
            "workload": row.get("workload"),
            "user_id": row.get("user_id", ""),
            "object_id": row.get("object_id", ""),
            "site_url": row.get("site_url"),
            "file_name": row.get("file_name"),
            "relative_path": row.get("relative_path"),
            "item_type": row.get("item_type", "File"),
            "sharing_type": row.get("sharing_type"),
            "sharing_scope": row.get("sharing_scope"),
            "sharing_permission": row.get("sharing_permission"),
            "event_time": row["event_time"].isoformat() if row.get("event_time") else None,
        }
        await redis_conn.rpush(QUEUE_KEY, json.dumps(job_payload))
        requeued += 1

    logger.info(
        "Startup recovery sweep complete: %d requeued, %d marked failed",
        requeued, failed,
    )


# -- Main loop ---------------------------------------------------------------

async def main() -> None:
    config = Config.from_env()
    setup_logging(service_name="worker", level=config.log_level)

    logger.info("Worker starting up")

    # Redis
    redis_conn = aioredis.from_url(config.redis_url, decode_responses=True)
    try:
        await redis_conn.ping()
        logger.info("Redis connection established")
    except Exception:
        logger.critical("Cannot reach Redis at %s", config.redis_url, exc_info=True)
        raise

    # PostgreSQL
    db_pool = await create_pool()
    await run_migrations(db_pool)
    logger.info("Database ready")

    # Reload config with DB overrides from admin panel
    from app.database.db_config import load_db_overrides
    db_overrides = await load_db_overrides(db_pool)
    if db_overrides:
        logger.info("Loaded %d DB config overrides", len(db_overrides))
        config = Config.from_env(db_overrides=db_overrides)

    # Recover events stuck in 'processing' from a prior crash/restart
    await recover_stuck_events(db_pool, redis_conn, config)

    # AI provider
    ai_provider = create_ai_provider(config)
    logger.info("AI provider ready: %s (%s)", config.ai_provider, ai_provider.get_model_name())

    # Second-look AI provider
    second_look_provider = create_second_look_provider(config)

    # Notification dispatcher
    notifier_dispatcher = create_notification_dispatcher(config)
    logger.info(
        "Notification dispatcher ready: %d channel(s)",
        len(notifier_dispatcher.notifiers),
    )

    # Graph auth (shared with orchestrator pipeline and remediation poller)
    from app.graph_api.auth import GraphAuth
    graph_auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )

    # Background tasks
    asyncio.create_task(heartbeat_loop(redis_conn))
    asyncio.create_task(cleanup_stale_files(config.tmpfs_path))

    from app.remediation.poller import remediation_poller
    asyncio.create_task(remediation_poller(db_pool, config, graph_auth, redis_conn))

    # User notification poller
    if config.user_notification_enabled:
        # Build AI provider for user notifications (may differ from main provider)
        notif_provider_name = config.user_notification_ai_provider or config.ai_provider
        notif_config = Config.from_env()
        notif_config.ai_provider = notif_provider_name
        # Override model if specified
        if config.user_notification_ai_model:
            model_field = {
                "anthropic": "anthropic_model",
                "openai": "openai_model",
                "gemini": "gemini_model",
            }.get(notif_provider_name.lower())
            if model_field:
                setattr(notif_config, model_field, config.user_notification_ai_model)
        user_notif_ai = create_ai_provider(notif_config)
        logger.info(
            "User notification AI provider: %s (%s)",
            notif_provider_name, user_notif_ai.get_model_name(),
        )

        from app.notifications.user_notification_poller import user_notification_poller
        asyncio.create_task(user_notification_poller(
            redis_conn=redis_conn,
            db_pool=db_pool,
            config=config,
            ai_provider=user_notif_ai,
            graph_auth=graph_auth,
        ))
    else:
        logger.info("User notifications disabled")

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received, finishing current job...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
    av_semaphore = asyncio.Semaphore(config.max_concurrent_av_jobs)
    active_tasks: set[asyncio.Task] = set()
    logger.info(
        "Listening for jobs on %s (max_concurrent=%d, max_av=%d)",
        QUEUE_KEY, config.max_concurrent_jobs, config.max_concurrent_av_jobs,
    )

    async def _run_job(job: dict[str, Any]) -> None:
        async with semaphore:
            try:
                await process_job(
                    job_data=job,
                    config=config,
                    db_pool=db_pool,
                    redis=redis_conn,
                    ai_provider=ai_provider,
                    notifier_dispatcher=notifier_dispatcher,
                    second_look_provider=second_look_provider,
                    av_semaphore=av_semaphore,
                )
            except Exception:
                logger.error(
                    "Error processing job %s", job.get("event_id", "?"),
                    exc_info=True,
                )

    while not shutdown_event.is_set():
        try:
            # BLPOP with 5-second timeout so we can check shutdown_event periodically
            result: Optional[list[Any]] = await redis_conn.blpop(QUEUE_KEY, timeout=5)  # type: ignore[assignment]
            if result is None:
                # Timeout -- no job available; loop back to check shutdown flag
                continue

            _key, job_json = result
            job: dict[str, Any] = json.loads(job_json)
            logger.info(
                "Received job: event_id=%s file=%s",
                job.get("event_id", "?"),
                job.get("file_name", "?"),
            )

            # Wait for a slot before accepting the next job
            await semaphore.acquire()
            semaphore.release()

            task = asyncio.create_task(_run_job(job))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON on queue: %s", exc)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.error("Unhandled exception in main loop", exc_info=True)
            await asyncio.sleep(5)

    # Shutdown: wait for in-flight jobs to finish
    if active_tasks:
        logger.info("Waiting for %d in-flight jobs to finish...", len(active_tasks))
        await asyncio.gather(*active_tasks, return_exceptions=True)

    logger.info("Worker shutting down")
    await redis_conn.aclose()
    await db_pool.close()
    logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
