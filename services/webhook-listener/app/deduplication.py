"""Redis-based deduplication for sharing events."""

import hashlib
import logging

import redis.asyncio as aioredis

from app.models import SharingEventResult

logger = logging.getLogger("webhook-listener")

DEDUP_KEY_PREFIX = "sharesentinel:dedup:"


def generate_dedup_key(result: SharingEventResult) -> str:
    """Generate a SHA-256 dedup key from ObjectId + Operation + CreationTime + UserId."""
    raw = (
        f"{result.ObjectId}"
        f"{result.Operation}"
        f"{result.CreationTime or ''}"
        f"{result.UserId}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def is_duplicate(
    redis_client: aioredis.Redis,
    dedup_hash: str,
    ttl_seconds: int,
) -> bool:
    """Check if an event is a duplicate using atomic SET NX.

    Returns True if the event has already been seen (duplicate).
    Returns False if the event is new (key was set successfully).
    """
    key = f"{DEDUP_KEY_PREFIX}{dedup_hash}"
    # SET key 1 EX ttl NX — returns True if set, None if already exists
    was_set = await redis_client.set(key, "1", ex=ttl_seconds, nx=True)
    if was_set:
        logger.debug("Dedup key set (new event)", extra={"dedup_hash": dedup_hash})
        return False
    logger.info("Duplicate event detected", extra={"dedup_hash": dedup_hash})
    return True
