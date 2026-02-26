"""Polls Microsoft Graph Audit Log Query API for sharing events.

Replaces Splunk webhook ingestion by directly querying the unified audit log
on an hourly schedule. New sharing events are pushed to the same Redis queue
the worker already consumes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import redis.asyncio as aioredis

from .config import LifecycleConfig
from .graph_api import GRAPH_BETA, DEFAULT_TIMEOUT, GraphAuth

logger = logging.getLogger(__name__)

QUEUE_KEY = "sharesentinel:jobs"
DEDUP_KEY_PREFIX = "sharesentinel:dedup:"
DEDUP_TTL_SECONDS = 86400  # 24 hours

# Audit query polling
QUERY_POLL_INTERVAL_S = 10
QUERY_TIMEOUT_S = 600  # 10 minutes max wait
OVERLAP_MINUTES = 5  # overlap window to avoid missing boundary events


class AuditLogPoller:
    """Polls Microsoft Graph Audit Log Query API for sharing events."""

    def __init__(
        self,
        auth: GraphAuth,
        redis_client: aioredis.Redis,
        db_pool: Any,  # asyncpg.Pool
        config: LifecycleConfig,
    ) -> None:
        self._auth = auth
        self._redis = redis_client
        self._db = db_pool
        self._operations = [
            op.strip() for op in config.audit_poll_operations.split(",") if op.strip()
        ]

    async def poll(self) -> dict:
        """Run one poll cycle: query audit logs since last run, push new events to Redis."""
        start_time = await self._get_last_poll_time()
        # Apply overlap window to avoid missing events at boundaries
        query_start = start_time - timedelta(minutes=OVERLAP_MINUTES)
        end_time = datetime.now(timezone.utc)

        logger.info(
            "Starting audit poll cycle: %s -> %s",
            query_start.isoformat(),
            end_time.isoformat(),
        )

        query_id = await self._create_audit_query(query_start, end_time)
        await self._wait_for_query_completion(query_id)
        records = await self._fetch_all_records(query_id)

        new_count = 0
        for record in records:
            job = self._transform_to_queue_job(record)
            if not await self._is_duplicate(job):
                await self._push_to_queue(job)
                new_count += 1

        await self._save_last_poll_time(end_time, new_count)
        logger.info("Audit poll complete: %d records, %d new jobs enqueued", len(records), new_count)
        return {"total": len(records), "new": new_count}

    # -- Audit Query API --------------------------------------------------------

    async def _create_audit_query(
        self, start: datetime, end: datetime
    ) -> str:
        """POST to create an audit log query. Returns the query ID."""
        url = f"{GRAPH_BETA}/security/auditLog/queries"
        headers = {
            "Authorization": f"Bearer {self._auth.get_access_token()}",
            "Content-Type": "application/json",
        }
        body = {
            "displayName": f"ShareSentinel poll {end.isoformat()}",
            "filterStartDateTime": start.isoformat(),
            "filterEndDateTime": end.isoformat(),
            "operationFilters": self._operations,
            "recordTypeFilters": ["sharePointSharingOperation"],
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        query_id = data["id"]
        logger.info("Audit query created: %s (status=%s)", query_id, data.get("status"))
        return query_id

    async def _wait_for_query_completion(self, query_id: str) -> None:
        """Poll GET until the query status is 'succeeded' or timeout."""
        url = f"{GRAPH_BETA}/security/auditLog/queries/{query_id}"
        headers = {"Authorization": f"Bearer {self._auth.get_access_token()}"}
        elapsed = 0

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while elapsed < QUERY_TIMEOUT_S:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status", "unknown")

                if status == "succeeded":
                    logger.info("Audit query %s succeeded", query_id)
                    return
                if status == "failed":
                    raise RuntimeError(f"Audit query {query_id} failed: {data}")

                logger.debug("Audit query %s status=%s, waiting...", query_id, status)
                await asyncio.sleep(QUERY_POLL_INTERVAL_S)
                elapsed += QUERY_POLL_INTERVAL_S
                # Refresh token for long waits
                headers = {"Authorization": f"Bearer {self._auth.get_access_token()}"}

        raise TimeoutError(
            f"Audit query {query_id} did not complete within {QUERY_TIMEOUT_S}s"
        )

    async def _fetch_all_records(self, query_id: str) -> list[dict]:
        """Paginate through all records for a completed query."""
        url = f"{GRAPH_BETA}/security/auditLog/queries/{query_id}/records"
        headers = {"Authorization": f"Bearer {self._auth.get_access_token()}"}
        all_records: list[dict] = []

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while url:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                all_records.extend(data.get("value", []))
                url = data.get("@odata.nextLink")
                # Refresh token between pages
                if url:
                    headers = {"Authorization": f"Bearer {self._auth.get_access_token()}"}

        logger.info("Fetched %d audit records", len(all_records))
        return all_records

    # -- Transform & Enqueue ----------------------------------------------------

    def _transform_to_queue_job(self, record: dict) -> dict:
        """Map an audit log record to a QueueJob-compatible dict."""
        # auditData is a JSON string in the record
        audit_data_raw = record.get("auditData", "{}")
        if isinstance(audit_data_raw, str):
            audit_data = json.loads(audit_data_raw)
        else:
            audit_data = audit_data_raw

        operation = record.get("operation", "")
        user_id = record.get("userPrincipalName", "") or record.get("userId", "")
        object_id = record.get("objectId", "")
        created = record.get("createdDateTime", "")

        # Compute event_id the same way as webhook-listener dedup
        event_id = hashlib.sha256(
            f"{object_id}{operation}{created}{user_id}".encode("utf-8")
        ).hexdigest()

        # Parse sharing details from auditData.EventData if available
        event_data = audit_data.get("EventData", "")
        sharing_type = "Unknown"
        sharing_scope = "Unknown"
        sharing_permission = "Unknown"
        if isinstance(event_data, str) and event_data:
            try:
                ed = json.loads(event_data)
                sharing_type = ed.get("SharingType", sharing_type)
                sharing_scope = ed.get("SharingScope", sharing_scope)
                sharing_permission = ed.get("SharingPermission", sharing_permission)
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(event_data, dict):
            sharing_type = event_data.get("SharingType", sharing_type)
            sharing_scope = event_data.get("SharingScope", sharing_scope)
            sharing_permission = event_data.get("SharingPermission", sharing_permission)

        return {
            "event_id": event_id,
            "operation": operation,
            "workload": record.get("service", "Unknown"),
            "user_id": user_id,
            "object_id": object_id,
            "site_url": audit_data.get("SiteUrl", ""),
            "file_name": audit_data.get("SourceFileName", ""),
            "relative_path": audit_data.get("SourceRelativeUrl", ""),
            "item_type": audit_data.get("ItemType", "File"),
            "sharing_type": sharing_type,
            "sharing_scope": sharing_scope,
            "sharing_permission": sharing_permission,
            "event_time": created,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "raw_payload": record,
        }

    async def _is_duplicate(self, job: dict) -> bool:
        """Check Redis dedup key (same logic as webhook-listener)."""
        key = f"{DEDUP_KEY_PREFIX}{job['event_id']}"
        was_set = await self._redis.set(key, "1", ex=DEDUP_TTL_SECONDS, nx=True)
        if was_set:
            return False
        logger.debug("Duplicate event skipped: %s", job["event_id"][:12])
        return True

    async def _push_to_queue(self, job: dict) -> None:
        """RPUSH job JSON to the worker queue."""
        await self._redis.rpush(QUEUE_KEY, json.dumps(job))
        logger.info(
            "Enqueued audit event: op=%s user=%s file=%s",
            job["operation"],
            job["user_id"],
            job["file_name"] or job["object_id"][:40],
        )

    # -- Poll State Persistence -------------------------------------------------

    async def _get_last_poll_time(self) -> datetime:
        """Read last_poll_time from Postgres. Falls back to 1 hour ago."""
        row = await self._db.fetchrow(
            "SELECT last_poll_time FROM audit_poll_state WHERE id = 1"
        )
        if row and row["last_poll_time"]:
            return row["last_poll_time"]
        return datetime.now(timezone.utc) - timedelta(hours=1)

    async def _save_last_poll_time(self, poll_time: datetime, events_found: int) -> None:
        """Upsert last_poll_time in Postgres."""
        await self._db.execute(
            """
            INSERT INTO audit_poll_state (id, last_poll_time, last_poll_status, events_found, updated_at)
            VALUES (1, $1, 'success', $2, NOW())
            ON CONFLICT (id) DO UPDATE SET
                last_poll_time = $1,
                last_poll_status = 'success',
                events_found = $2,
                updated_at = NOW()
            """,
            poll_time,
            events_found,
        )
