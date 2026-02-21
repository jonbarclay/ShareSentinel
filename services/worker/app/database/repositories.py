"""Repository classes for database access using asyncpg."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from ..ai.base_provider import AnalysisResponse

logger = logging.getLogger(__name__)


class EventRepository:
    """CRUD operations for the ``events`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_event(self, job: Any) -> Optional[int]:
        """Insert a new event from a queue job and return the row id.

        Returns ``None`` if the event already exists (duplicate).

        ``job`` must expose attributes: event_id, operation, user_id,
        object_id, item_type, and optionally workload, file_name,
        relative_path, site_url, sharing_type, sharing_scope,
        sharing_permission, event_time, raw_payload.
        """
        raw = getattr(job, "raw_payload", None)
        if raw is not None and not isinstance(raw, str):
            raw = json.dumps(raw) if not isinstance(raw, (dict, list)) else json.dumps(raw)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO events (
                    event_id, operation, workload, user_id, object_id,
                    site_url, file_name, relative_path, item_type,
                    sharing_type, sharing_scope, sharing_permission,
                    event_time, status, processing_started_at, raw_payload
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12,
                    $13, $14, $15, $16
                )
                ON CONFLICT (event_id) DO NOTHING
                RETURNING id
                """,
                getattr(job, "event_id", ""),
                getattr(job, "operation", ""),
                getattr(job, "workload", None),
                getattr(job, "user_id", ""),
                getattr(job, "object_id", ""),
                getattr(job, "site_url", None),
                getattr(job, "file_name", None),
                getattr(job, "relative_path", None),
                getattr(job, "item_type", ""),
                getattr(job, "sharing_type", None),
                getattr(job, "sharing_scope", None),
                getattr(job, "sharing_permission", None),
                getattr(job, "event_time", None) or None,
                "processing",
                datetime.now(timezone.utc),
                raw,
            )
            if row is None:
                logger.info("Duplicate event_id=%s, skipping", getattr(job, "event_id", ""))
                return None
            logger.info("Created event record id=%s event_id=%s", row["id"], getattr(job, "event_id", ""))
            return row["id"]

    async def update_event_status(self, event_id: str, status: str, **kwargs: Any) -> None:
        """Update the event status and any extra columns supplied via *kwargs*."""
        sets = ["status = $1", "updated_at = $2"]
        params: list[Any] = [status, datetime.now(timezone.utc)]
        idx = 3

        if status == "completed" and "processing_completed_at" not in kwargs:
            kwargs["processing_completed_at"] = datetime.now(timezone.utc)

        for col, val in kwargs.items():
            sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1

        params.append(event_id)
        query = f"UPDATE events SET {', '.join(sets)} WHERE event_id = ${idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def update_event_metadata(self, event_id: str, metadata: Dict[str, Any]) -> None:
        """Update Graph API metadata columns on the event."""
        if not metadata:
            return

        sets: list[str] = ["updated_at = $1"]
        params: list[Any] = [datetime.now(timezone.utc)]
        idx = 2

        allowed = {
            "confirmed_file_name", "file_size_bytes", "mime_type",
            "web_url", "sharing_link_url", "drive_id", "item_id_graph",
            "sharing_links",
        }
        jsonb_cols = {"sharing_links"}
        for col, val in metadata.items():
            if col not in allowed:
                continue
            if col in jsonb_cols:
                sets.append(f"{col} = ${idx}::jsonb")
                params.append(json.dumps(val) if val is not None else None)
            else:
                sets.append(f"{col} = ${idx}")
                params.append(val)
            idx += 1

        params.append(event_id)
        query = f"UPDATE events SET {', '.join(sets)} WHERE event_id = ${idx}"

        async with self._pool.acquire() as conn:
            await conn.execute(query, *params)

    async def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Return the event row as a dict, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM events WHERE event_id = $1", event_id)
            return dict(row) if row else None

    async def get_events_by_status(self, status: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Return events matching *status* ordered by received_at."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM events WHERE status = $1 ORDER BY received_at LIMIT $2",
                status,
                limit,
            )
            return [dict(r) for r in rows]


class VerdictRepository:
    """CRUD operations for the ``verdicts`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_verdict(
        self,
        event_id: str,
        response: AnalysisResponse,
        analysis_mode: str,
        notification_required: bool,
    ) -> int:
        """Insert an AI verdict and return the row id."""
        categories = json.dumps(response.categories_detected)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO verdicts (
                    event_id, sensitivity_rating, categories_detected,
                    summary, confidence, recommendation,
                    analysis_mode, ai_provider, ai_model,
                    input_tokens, output_tokens, estimated_cost_usd,
                    processing_time_seconds, notification_required
                ) VALUES (
                    $1, $2, $3::jsonb,
                    $4, $5, $6,
                    $7, $8, $9,
                    $10, $11, $12,
                    $13, $14
                )
                RETURNING id
                """,
                event_id,
                response.sensitivity_rating,
                categories,
                response.summary,
                response.confidence,
                response.recommendation,
                analysis_mode,
                response.provider,
                response.model,
                response.input_tokens,
                response.output_tokens,
                response.estimated_cost_usd,
                response.processing_time_seconds,
                notification_required,
            )
            logger.info(
                "Created verdict id=%s event_id=%s rating=%s",
                row["id"], event_id, response.sensitivity_rating,
            )
            return row["id"]

    async def update_notification_status(
        self,
        event_id: str,
        sent: bool,
        channel: str,
        reference: Optional[str] = None,
    ) -> None:
        """Record that a notification was sent (or failed) for this verdict."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE verdicts
                SET notification_sent = $1,
                    notification_sent_at = $2,
                    notification_channel = $3,
                    notification_reference = $4
                WHERE event_id = $5
                """,
                sent,
                datetime.now(timezone.utc) if sent else None,
                channel,
                reference,
                event_id,
            )

    async def get_verdict(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Return the verdict row for *event_id*, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM verdicts WHERE event_id = $1", event_id)
            return dict(row) if row else None


class FileHashRepository:
    """CRUD operations for the ``file_hashes`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def check_hash(self, file_hash: str, max_age_days: int = 30) -> Optional[Dict[str, Any]]:
        """Check if *file_hash* exists and was seen within *max_age_days*.

        Returns the row as a dict or None.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM file_hashes
                WHERE file_hash = $1
                  AND last_seen_at > NOW() - make_interval(days => $2)
                """,
                file_hash,
                max_age_days,
            )
            return dict(row) if row else None

    async def store_hash(self, file_hash: str, event_id: str, sensitivity_rating: int) -> None:
        """Insert a new file hash record."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO file_hashes (file_hash, first_event_id, sensitivity_rating)
                VALUES ($1, $2, $3)
                ON CONFLICT (file_hash) DO UPDATE
                    SET times_seen = file_hashes.times_seen + 1,
                        last_seen_at = NOW()
                """,
                file_hash,
                event_id,
                sensitivity_rating,
            )

    async def update_last_seen(self, file_hash: str) -> None:
        """Bump *last_seen_at* and increment *times_seen*."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE file_hashes
                SET last_seen_at = NOW(),
                    times_seen = times_seen + 1
                WHERE file_hash = $1
                """,
                file_hash,
            )


class UserProfileRepository:
    """CRUD operations for the ``user_profiles`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_cached(self, user_id: str, max_age_days: int = 7) -> Optional[Dict[str, Any]]:
        """Return cached profile if fetched within *max_age_days*, else None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM user_profiles
                WHERE user_id = $1
                  AND fetched_at > NOW() - make_interval(days => $2)
                """,
                user_id,
                max_age_days,
            )
            return dict(row) if row else None

    async def upsert(self, user_id: str, profile: Dict[str, Any]) -> None:
        """Insert or update a user profile."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_profiles (
                    user_id, display_name, job_title, department,
                    mail, manager_name, photo_base64, fetched_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    job_title = EXCLUDED.job_title,
                    department = EXCLUDED.department,
                    mail = EXCLUDED.mail,
                    manager_name = EXCLUDED.manager_name,
                    photo_base64 = EXCLUDED.photo_base64,
                    updated_at = NOW(),
                    fetched_at = NOW()
                """,
                user_id,
                profile.get("display_name"),
                profile.get("job_title"),
                profile.get("department"),
                profile.get("mail"),
                profile.get("manager_name"),
                profile.get("photo_base64"),
            )

    async def get_by_user_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Simple lookup with no staleness check."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_profiles WHERE user_id = $1", user_id
            )
            return dict(row) if row else None


class AuditLogRepository:
    """Append-only operations for the ``audit_log`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def log(
        self,
        event_id: Optional[str],
        action: str,
        details: Optional[Dict[str, Any]] = None,
        status: str = "success",
        error: Optional[str] = None,
    ) -> None:
        """Write an audit log entry."""
        details_json = json.dumps(details) if details else None
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_log (event_id, action, details, status, error_message)
                    VALUES ($1, $2, $3::jsonb, $4, $5)
                    """,
                    event_id,
                    action,
                    details_json,
                    status,
                    error,
                )
        except Exception:
            # Audit logging should never break the pipeline
            logger.exception("Failed to write audit log entry action=%s event_id=%s", action, event_id)

    async def get_logs_for_event(self, event_id: str) -> List[Dict[str, Any]]:
        """Return all audit log entries for *event_id* ordered by creation time."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE event_id = $1 ORDER BY created_at",
                event_id,
            )
            return [dict(r) for r in rows]
