"""Background loop that consumes user notification jobs from Redis."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import asyncpg
import redis.asyncio as aioredis

from ..ai.base_provider import BaseAIProvider
from ..config import Config
from ..graph_api.auth import GraphAuth
from .user_notifier import UserNotifier

logger = logging.getLogger(__name__)

QUEUE_KEY = "sharesentinel:user_notifications"


async def user_notification_poller(
    redis_conn: aioredis.Redis,
    db_pool: asyncpg.Pool,
    config: Config,
    ai_provider: BaseAIProvider,
    graph_auth: GraphAuth,
) -> None:
    """Continuously consume from the user notifications Redis queue.

    Each message is a JSON dict with ``event_id`` and ``disposition``.
    """
    if not config.user_notification_enabled:
        logger.info("User notifications disabled — poller will not start")
        return

    notifier = UserNotifier(
        config=config,
        db_pool=db_pool,
        ai_provider=ai_provider,
        graph_auth=graph_auth,
    )

    logger.info("User notification poller started (queue=%s)", QUEUE_KEY)

    while True:
        try:
            result: Optional[list[Any]] = await redis_conn.blpop(QUEUE_KEY, timeout=10)
            if result is None:
                continue

            _key, msg_json = result
            msg = json.loads(msg_json)
            event_id = msg.get("event_id", "")
            disposition = msg.get("disposition", "")

            if not event_id or not disposition:
                logger.warning("Invalid user notification message: %s", msg_json)
                continue

            logger.info(
                "Processing user notification: event_id=%s disposition=%s",
                event_id, disposition,
            )

            try:
                await notifier.send_user_notification(event_id, disposition)
            except Exception:
                logger.exception(
                    "Failed to process user notification for event %s", event_id
                )

        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON on user notification queue: %s", exc)
        except asyncio.CancelledError:
            logger.info("User notification poller cancelled")
            break
        except Exception:
            logger.error("Error in user notification poller loop", exc_info=True)
            await asyncio.sleep(5)
