"""Redis queue integration for pushing jobs to the worker."""

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.models import QueueJob, SplunkWebhookPayload

logger = logging.getLogger("webhook-listener")

QUEUE_KEY = "sharesentinel:jobs"


def build_queue_job(
    payload: SplunkWebhookPayload,
    event_id: str,
) -> QueueJob:
    """Build a QueueJob from a validated SplunkWebhookPayload."""
    result = payload.result
    return QueueJob(
        event_id=event_id,
        operation=result.Operation,
        workload=result.Workload or "Unknown",
        user_id=result.UserId,
        object_id=result.ObjectId,
        site_url=result.SiteUrl or "",
        file_name=result.SourceFileName or "",
        relative_path=result.SourceRelativeUrl or "",
        item_type=result.ItemType,
        sharing_type=result.SharingType or "Unknown",
        sharing_scope=result.SharingScope or "Unknown",
        sharing_permission=result.SharingPermission or "Unknown",
        event_time=result.CreationTime or "",
        received_at=datetime.now(timezone.utc).isoformat(),
        raw_payload=payload.model_dump(),
    )


async def enqueue_job(
    redis_client: aioredis.Redis,
    job: QueueJob,
) -> int:
    """Serialize and RPUSH a QueueJob to the Redis queue.

    Returns the length of the queue after the push.
    """
    job_json = job.model_dump_json()
    queue_length = await redis_client.rpush(QUEUE_KEY, job_json)
    logger.info(
        "Job enqueued",
        extra={
            "event_id": job.event_id,
            "operation": job.operation,
            "user_id": job.user_id,
            "file_name": job.file_name,
            "item_type": job.item_type,
            "queue_length": queue_length,
        },
    )
    return queue_length
