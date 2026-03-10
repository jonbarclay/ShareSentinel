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
QUERY_TIMEOUT_S = 1800  # 30 minutes max wait (Graph beta API is slow)
OVERLAP_MINUTES = 5  # overlap window to avoid missing boundary events
MAX_QUERY_WINDOW_MINUTES = 120  # chunk large windows; Graph API latency is per-query not per-window


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
        """Run one poll cycle: query audit logs since last run, push new events to Redis.

        If the time window since the last successful poll exceeds
        MAX_QUERY_WINDOW_MINUTES, the window is broken into smaller chunks
        to avoid Graph API query timeouts on large ranges.
        """
        start_time = await self._get_last_poll_time()
        end_time = datetime.now(timezone.utc)
        total_gap = end_time - start_time

        if total_gap > timedelta(minutes=MAX_QUERY_WINDOW_MINUTES + OVERLAP_MINUTES):
            logger.info(
                "Large poll gap detected (%.1f hours). Chunking into %d-minute windows.",
                total_gap.total_seconds() / 3600,
                MAX_QUERY_WINDOW_MINUTES,
            )
            return await self._poll_chunked(start_time, end_time)

        return await self._poll_window(start_time, end_time, save_state=True)

    async def _poll_chunked(self, start_time: datetime, final_end: datetime) -> dict:
        """Process a large time gap in sequential chunks."""
        total_records = 0
        total_new = 0
        chunk_start = start_time

        while chunk_start < final_end:
            chunk_end = min(
                chunk_start + timedelta(minutes=MAX_QUERY_WINDOW_MINUTES),
                final_end,
            )
            logger.info(
                "Processing chunk: %s -> %s",
                chunk_start.isoformat(),
                chunk_end.isoformat(),
            )
            stats = await self._poll_window(chunk_start, chunk_end, save_state=True)
            total_records += stats["total"]
            total_new += stats["new"]
            # Advance to next chunk (the saved poll time is chunk_end)
            chunk_start = chunk_end

        logger.info(
            "Chunked poll complete: %d total records, %d new jobs across all chunks",
            total_records, total_new,
        )
        return {"total": total_records, "new": total_new}

    async def _poll_window(
        self, start_time: datetime, end_time: datetime, *, save_state: bool
    ) -> dict:
        """Query a single time window and enqueue new events."""
        # Apply overlap window to avoid missing events at boundaries
        query_start = start_time - timedelta(minutes=OVERLAP_MINUTES)

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

        if save_state:
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
                try:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (502, 503, 504, 429):
                        logger.warning(
                            "Transient %d polling query %s, retrying...",
                            e.response.status_code, query_id,
                        )
                        await asyncio.sleep(QUERY_POLL_INTERVAL_S)
                        elapsed += QUERY_POLL_INTERVAL_S
                        headers = {"Authorization": f"Bearer {self._auth.get_access_token()}"}
                        continue
                    raise

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
            "raw_payload": self._cap_payload(record),
        }

    @staticmethod
    def _cap_payload(record: dict, max_bytes: int = 32768) -> dict:
        """Cap raw_payload size to prevent unbounded JSONB storage.

        If the serialized payload exceeds *max_bytes*, replace with a
        truncation marker preserving key metadata fields.
        """
        serialized = json.dumps(record)
        if len(serialized.encode("utf-8")) <= max_bytes:
            return record
        return {
            "_truncated": True,
            "_original_size": len(serialized.encode("utf-8")),
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
            INSERT INTO audit_poll_state (id, last_poll_time, last_poll_status, events_found, error_message, updated_at)
            VALUES (1, $1, 'success', $2, NULL, NOW())
            ON CONFLICT (id) DO UPDATE SET
                last_poll_time = $1,
                last_poll_status = 'success',
                events_found = $2,
                error_message = NULL,
                updated_at = NOW()
            """,
            poll_time,
            events_found,
        )

    async def save_poll_error(self, error_text: str) -> None:
        """Record a poll failure in audit_poll_state.

        Updates updated_at so the watchdog can distinguish 'dead' (no updates)
        from 'alive but failing' (recent updates with error status).
        """
        # Cap error text to avoid unbounded storage
        truncated = error_text[:2000] if error_text else "unknown error"
        try:
            await self._db.execute(
                """
                INSERT INTO audit_poll_state (id, last_poll_status, error_message, updated_at)
                VALUES (1, 'error', $1, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    last_poll_status = 'error',
                    error_message = $1,
                    updated_at = NOW()
                """,
                truncated,
            )
        except Exception:
            logger.warning("Failed to save poll error to DB", exc_info=True)
