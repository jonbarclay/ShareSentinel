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
from .provisioning_enforcer import (
    PROVISIONING_OPERATIONS,
    PROVISIONING_QUERY_OPERATIONS,
    PROVISIONING_QUERY_RECORD_TYPES,
    ProvisioningEnforcer,
)

logger = logging.getLogger(__name__)

QUEUE_KEY = "sharesentinel:jobs"
DEDUP_KEY_PREFIX = "sharesentinel:dedup:"
# Must comfortably exceed the reconciliation look-back window, or re-read
# records would be re-enqueued as new events.
DEDUP_TTL_SECONDS = 3 * 86400  # 72 hours

# Reconciliation pass: unified audit log records routinely become queryable
# 30 minutes to several hours AFTER the activity they describe. The normal
# incremental poll (5-min overlap) permanently misses those, so every
# ``reconcile_interval`` we re-query the trailing ``lookback`` window and rely
# on dedup to drop the already-seen majority.
RECON_LAST_KEY = "sharesentinel:audit_recon_last"

# In-flight query tracking. Graph audit queries can run longer than our wait
# timeout during API slowdowns; instead of abandoning a slow query and
# re-creating an identical one every cycle (which never converges), remember
# the query id per time window and resume waiting on it next cycle.
# The value is JSON {"id": ..., "timeouts": n}; after MAX_QUERY_RESUMES
# timed-out waits the query is treated as a server-side zombie and abandoned
# (observed 2026-07-06: a query stayed status=running for 10+ hours and the
# resume loop pinned the checkpoint in place indefinitely).
INFLIGHT_QUERY_KEY_PREFIX = "sharesentinel:audit_query_inflight:"
INFLIGHT_QUERY_TTL_S = 86400  # Graph retains queries long enough; 24h is ample
MAX_QUERY_RESUMES = 2

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
        self._config = config
        self._operations = [
            op.strip() for op in config.audit_poll_operations.split(",") if op.strip()
        ]
        self._provisioning: ProvisioningEnforcer | None = None
        if config.audit_poll_provisioning_enabled:
            self._provisioning = ProvisioningEnforcer(
                auth, redis_client, db_pool, config
            )
            logger.info(
                "Provisioning event ingestion enabled (operations: %s)",
                ", ".join(PROVISIONING_QUERY_OPERATIONS),
            )

    async def poll(self) -> dict:
        """Run one poll cycle: query audit logs since last run, push new events to Redis.

        If the time window since the last successful poll exceeds
        MAX_QUERY_WINDOW_MINUTES, the window is broken into smaller chunks
        to avoid Graph API query timeouts on large ranges.

        After the incremental window, a reconciliation pass re-queries the
        trailing look-back window every ``audit_poll_reconcile_interval_hours``
        to catch records that surfaced in the audit log late (dedup drops the
        already-seen ones). Reconciliation failures never fail the poll cycle.
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
            stats = await self._poll_chunked(start_time, end_time, save_state=True)
        else:
            stats = await self._poll_window(start_time, end_time, save_state=True)

        try:
            recon = await self._maybe_reconcile(end_time)
            if recon:
                stats["recon_total"] = recon["total"]
                stats["recon_new"] = recon["new"]
        except Exception:
            logger.exception("Reconciliation pass failed (incremental poll unaffected)")

        return stats

    async def _maybe_reconcile(self, now: datetime) -> dict | None:
        """Re-query the trailing look-back window if the interval has elapsed."""
        lookback_h = self._config.audit_poll_lookback_hours
        interval_h = self._config.audit_poll_reconcile_interval_hours
        if lookback_h <= 0 or interval_h <= 0:
            return None
        # Look-back must stay inside the dedup TTL or re-read records would
        # re-enqueue as new events.
        max_lookback_h = DEDUP_TTL_SECONDS // 3600 - 12
        if lookback_h > max_lookback_h:
            logger.warning(
                "audit_poll_lookback_hours=%d exceeds safe maximum %d; clamping",
                lookback_h, max_lookback_h,
            )
            lookback_h = max_lookback_h

        try:
            raw = await self._redis.get(RECON_LAST_KEY)
            last_recon = float(raw) if raw else 0.0
        except Exception:
            last_recon = 0.0
        if now.timestamp() - last_recon < interval_h * 3600:
            return None

        start = now - timedelta(hours=lookback_h)
        logger.info(
            "Reconciliation pass: re-querying %s -> %s for late-arriving records",
            start.isoformat(), now.isoformat(),
        )
        stats = await self._poll_chunked(start, now, save_state=False)
        if stats["new"]:
            logger.warning(
                "Reconciliation found %d late-arriving event(s) the incremental "
                "poll had missed", stats["new"],
            )
        try:
            await self._redis.set(RECON_LAST_KEY, str(now.timestamp()))
        except Exception:
            logger.warning("Failed to persist reconciliation timestamp", exc_info=True)
        return stats

    async def _poll_chunked(
        self, start_time: datetime, final_end: datetime, *, save_state: bool
    ) -> dict:
        """Process a large time gap in sequential chunks."""
        total_records = 0
        total_new = 0
        total_provisioning = 0
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
            stats = await self._poll_window(chunk_start, chunk_end, save_state=save_state)
            total_records += stats["total"]
            total_new += stats["new"]
            total_provisioning += stats.get("provisioning", 0)
            # Advance to next chunk (the saved poll time is chunk_end)
            chunk_start = chunk_end
            # Liveness: a multi-chunk pass (catch-up or reconciliation) can
            # exceed the watchdog's poll-staleness threshold without touching
            # audit_poll_state — which got the container auto-restarted
            # mid-pass on 2026-07-05, killing an unrelated 9-hour CWS sweep.
            # Refresh updated_at between chunks so "working" isn't "stale".
            if not save_state:
                await self._touch_poll_state()

        logger.info(
            "Chunked poll complete: %d total records, %d new jobs across all chunks",
            total_records, total_new,
        )
        return {
            "total": total_records,
            "new": total_new,
            "provisioning": total_provisioning,
        }

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

        inflight_key = (
            f"{INFLIGHT_QUERY_KEY_PREFIX}"
            f"{query_start.isoformat()}|{end_time.isoformat()}"
        )
        query_id = await self._get_or_create_query(
            inflight_key, query_start, end_time
        )
        try:
            await self._wait_for_query_completion(query_id)
        except TimeoutError:
            # Count the timed-out wait so a zombie query is abandoned after
            # MAX_QUERY_RESUMES instead of pinning the checkpoint forever.
            await self._record_query_timeout(inflight_key, query_id)
            raise
        records = await self._fetch_all_records(query_id)
        # Window fully processed — drop the in-flight marker.
        try:
            await self._redis.delete(inflight_key)
        except Exception:
            logger.debug("Failed to clear in-flight query key", exc_info=True)

        new_count = 0
        provisioning_count = 0
        for record in records:
            job = self._transform_to_queue_job(record)
            if await self._is_seen(job):
                continue
            operation = (record.get("operation") or "").strip().lower()
            if self._provisioning is not None and operation in PROVISIONING_OPERATIONS:
                # Creation events are policy triggers, not file-analysis jobs:
                # route them to the provisioning enforcer instead of the worker.
                await self._provisioning.handle_record(record, job)
                await self._mark_seen(job)
                provisioning_count += 1
                continue
            # Push BEFORE marking seen: a crash between the two re-delivers
            # the event next cycle (worker create_event dedups on conflict),
            # whereas mark-then-push would silently lose it for the dedup TTL.
            await self._push_to_queue(job)
            await self._mark_seen(job)
            new_count += 1

        if save_state:
            await self._save_last_poll_time(end_time, new_count + provisioning_count)
        logger.info(
            "Audit poll complete: %d records, %d new jobs enqueued, "
            "%d provisioning events handled",
            len(records), new_count, provisioning_count,
        )
        return {
            "total": len(records),
            "new": new_count,
            "provisioning": provisioning_count,
        }

    # -- Audit Query API --------------------------------------------------------

    async def _get_or_create_query(
        self, inflight_key: str, query_start: datetime, end_time: datetime
    ) -> str:
        """Reuse a still-viable in-flight query for this window, else create one.

        A query that timed out our wait in a previous cycle usually keeps
        running server-side; resuming it lets slow cycles converge instead of
        restarting Graph's work from zero every 15 minutes. But a query that
        has timed out MAX_QUERY_RESUMES waits is a server-side zombie —
        abandon it and create a fresh one, or the checkpoint never advances.
        """
        existing: str | None = None
        timeouts = 0
        try:
            raw = await self._redis.get(inflight_key)
            if raw:
                text = raw.decode() if isinstance(raw, bytes) else str(raw)
                try:
                    payload = json.loads(text)
                    existing = payload.get("id")
                    timeouts = int(payload.get("timeouts", 0))
                except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                    existing = text  # legacy raw-id format
        except Exception:
            logger.debug("Failed to read in-flight query key", exc_info=True)

        if existing and timeouts >= MAX_QUERY_RESUMES:
            logger.warning(
                "Abandoning audit query %s after %d timed-out waits — "
                "creating a fresh query for this window",
                existing, timeouts,
            )
            existing = None

        if existing:
            status = await self._get_query_status(existing)
            if status in ("succeeded", "running", "notStarted"):
                logger.info(
                    "Resuming in-flight audit query %s (status=%s, timeouts=%d)",
                    existing, status, timeouts,
                )
                return existing
            logger.info(
                "Stored audit query %s not reusable (status=%s) — creating new",
                existing, status,
            )

        query_id = await self._create_audit_query(query_start, end_time)
        try:
            await self._redis.set(
                inflight_key,
                json.dumps({"id": query_id, "timeouts": 0}),
                ex=INFLIGHT_QUERY_TTL_S,
            )
        except Exception:
            logger.debug("Failed to store in-flight query key", exc_info=True)
        return query_id

    async def _record_query_timeout(self, inflight_key: str, query_id: str) -> None:
        """Bump the timed-out-wait counter for the window's in-flight query."""
        try:
            raw = await self._redis.get(inflight_key)
            timeouts = 0
            if raw:
                text = raw.decode() if isinstance(raw, bytes) else str(raw)
                try:
                    timeouts = int(json.loads(text).get("timeouts", 0))
                except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                    timeouts = 0
            await self._redis.set(
                inflight_key,
                json.dumps({"id": query_id, "timeouts": timeouts + 1}),
                ex=INFLIGHT_QUERY_TTL_S,
            )
        except Exception:
            logger.debug("Failed to record query timeout", exc_info=True)

    async def _get_query_status(self, query_id: str) -> str:
        """Fetch a query's status; 'missing' on 404, 'unknown' on any error."""
        url = f"{GRAPH_BETA}/security/auditLog/queries/{query_id}"
        headers = {"Authorization": f"Bearer {self._auth.get_access_token()}"}
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return "missing"
            resp.raise_for_status()
            return resp.json().get("status", "unknown")
        except Exception:
            logger.debug("Status check failed for query %s", query_id, exc_info=True)
            return "unknown"

    def _query_filters(self) -> tuple[list[str], list[str]]:
        """Build (operationFilters, recordTypeFilters) for the audit query.

        With provisioning ingestion enabled, the single query also matches
        site/group/user creation events. Operation and record-type filters are
        ANDed by the API, but each operation only exists under one record
        type, so the combined filter never produces false matches.
        """
        operations = list(self._operations)
        record_types = ["sharePointSharingOperation"]
        if self._provisioning is not None:
            operations += PROVISIONING_QUERY_OPERATIONS
            record_types += PROVISIONING_QUERY_RECORD_TYPES
        return operations, record_types

    async def _create_audit_query(
        self, start: datetime, end: datetime
    ) -> str:
        """POST to create an audit log query. Returns the query ID."""
        url = f"{GRAPH_BETA}/security/auditLog/queries"
        headers = {
            "Authorization": f"Bearer {self._auth.get_access_token()}",
            "Content-Type": "application/json",
        }
        operations, record_types = self._query_filters()
        body = {
            "displayName": f"ShareSentinel poll {end.isoformat()}",
            "filterStartDateTime": start.isoformat(),
            "filterEndDateTime": end.isoformat(),
            "operationFilters": operations,
            "recordTypeFilters": record_types,
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            if (
                resp.status_code in (400, 403)
                and self._provisioning is not None
            ):
                # Most likely cause: the app registration holds only the
                # SharePoint-scoped audit permission, which cannot query
                # azureActiveDirectory records. Fall back to the sharing-only
                # query so link-event ingestion keeps working, and surface
                # the configuration problem loudly.
                logger.error(
                    "Audit query with provisioning filters rejected "
                    "(HTTP %d: %s). Falling back to sharing-only filters for "
                    "this cycle. Grant the app AuditLogsQuery.Read.All (the "
                    "SharePoint-scoped variant is insufficient) or set "
                    "AUDIT_POLL_PROVISIONING_ENABLED=false.",
                    resp.status_code, resp.text[:300],
                )
                body["operationFilters"] = list(self._operations)
                body["recordTypeFilters"] = ["sharePointSharingOperation"]
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

    async def _is_seen(self, job: dict) -> bool:
        """Check the dedup key WITHOUT claiming it — the claim happens in
        _mark_seen() only after the event was durably enqueued/handled."""
        key = f"{DEDUP_KEY_PREFIX}{job['event_id']}"
        exists = await self._redis.exists(key)
        if exists:
            logger.debug("Duplicate event skipped: %s", job["event_id"][:12])
            return True
        return False

    async def _mark_seen(self, job: dict) -> None:
        key = f"{DEDUP_KEY_PREFIX}{job['event_id']}"
        await self._redis.set(key, "1", ex=DEDUP_TTL_SECONDS)

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

    async def _touch_poll_state(self) -> None:
        """Refresh updated_at only — signals liveness to the watchdog without
        advancing the checkpoint or changing the last status."""
        try:
            await self._db.execute(
                "UPDATE audit_poll_state SET updated_at = NOW() WHERE id = 1"
            )
        except Exception:
            logger.debug("Failed to touch poll state", exc_info=True)

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
