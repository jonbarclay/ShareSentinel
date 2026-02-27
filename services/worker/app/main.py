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
            max_tokens=config.ai_max_tokens,
            temperature=config.ai_temperature,
        )
    elif provider_name == "openai":
        return OpenAIProvider(
            api_key=config.openai_api_key,
            model=config.openai_model,
            prompt_manager=prompt_manager,
            max_tokens=config.ai_max_tokens,
            temperature=config.ai_temperature,
        )
    elif provider_name == "gemini":
        return GeminiProvider(
            api_key=config.gemini_api_key,
            model=config.gemini_model,
            prompt_manager=prompt_manager,
            max_tokens=config.ai_max_tokens,
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
            max_tokens=config.ai_max_tokens,
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
    asyncio.create_task(remediation_poller(db_pool, config, graph_auth))

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received, finishing current job...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
    active_tasks: set[asyncio.Task] = set()
    logger.info(
        "Listening for jobs on %s (max_concurrent=%d)",
        QUEUE_KEY, config.max_concurrent_jobs,
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
