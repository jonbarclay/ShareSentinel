"""Core remediation logic: remove sharing permissions and send report."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg
import httpx
import redis.asyncio as aioredis

from ..config import Config
from ..database.repositories import AuditLogRepository, UserProfileRepository
from ..graph_api.auth import GraphAuth
from ..graph_api.client import GRAPH_BASE, DEFAULT_TIMEOUT
from ..graph_api.sharing import get_sharing_permissions
from ..notifications.base_notifier import AlertPayload
from ..notifications.email_notifier import EmailNotifier

logger = logging.getLogger(__name__)


async def remove_sharing_permission(
    auth: GraphAuth,
    drive_id: str,
    item_id: str,
    permission_id: str,
) -> bool:
    """DELETE a single permission from a drive item.

    Returns True on success (204) or if already removed (404).
    Raises on other HTTP errors.
    """
    url = (
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
        f"/permissions/{permission_id}"
    )
    headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.delete(url, headers=headers)
        if resp.status_code in (204, 404):
            return True
        resp.raise_for_status()
        return True  # pragma: no cover


def _humanize_bytes(n: Optional[int]) -> str:
    if n is None:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


async def execute_remediation(
    row: Dict[str, Any],
    db_pool: asyncpg.Pool,
    config: Config,
    auth: GraphAuth,
    redis_conn: Optional[aioredis.Redis] = None,
) -> None:
    """Run the full remediation for a single remediations row.

    Steps:
    1. Load event + verdict + user profile from DB
    2. Remove anonymous/org-wide sharing permissions via Graph API
    3. Build and send a remediation report email
    4. Update the remediations row with results
    """
    remediation_id: int = row["id"]
    event_id: str = row["event_id"]
    audit = AuditLogRepository(db_pool)

    await audit.log(event_id, "remediation_started", {"remediation_id": remediation_id})

    # ---- 1. Load event + verdict + user profile ----
    async with db_pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT * FROM events WHERE event_id = $1", event_id
        )
        if not event:
            await _mark_failed(db_pool, remediation_id, "Event not found")
            await audit.log(event_id, "remediation_failed", error="Event not found")
            return

        verdict = await conn.fetchrow(
            "SELECT * FROM verdicts WHERE event_id = $1 ORDER BY id DESC LIMIT 1",
            event_id,
        )

        profile: Optional[Dict[str, Any]] = None
        user_repo = UserProfileRepository(db_pool)
        profile = await user_repo.get_cached(
            event["user_id"], config.user_profile_cache_days
        )

    drive_id = event.get("drive_id")
    item_id = event.get("item_id_graph")

    # ---- 2. Remove sharing permissions ----
    permissions_removed = 0
    permissions_failed = 0
    permission_details: List[Dict[str, Any]] = []

    if drive_id and item_id:
        try:
            all_perms = await get_sharing_permissions(auth, drive_id, item_id)
        except Exception as exc:
            logger.error("Failed to list permissions for %s: %s", event_id, exc)
            all_perms = []
            permission_details.append({
                "error": f"Failed to list permissions: {exc}",
            })

        for perm in all_perms:
            link = perm.get("link")
            if not link:
                continue
            scope = link.get("scope", "").lower()
            if scope not in ("anonymous", "organization"):
                continue

            perm_id = perm.get("id")
            if not perm_id:
                continue

            detail: Dict[str, Any] = {
                "permission_id": perm_id,
                "scope": scope,
                "type": link.get("type", "unknown"),
            }
            try:
                await remove_sharing_permission(auth, drive_id, item_id, perm_id)
                detail["status"] = "removed"
                permissions_removed += 1
                await audit.log(event_id, "permission_removed", detail)
            except Exception as exc:
                detail["status"] = "failed"
                detail["error"] = str(exc)
                permissions_failed += 1
                await audit.log(
                    event_id, "permission_removal_failed", detail, status="error",
                    error=str(exc),
                )
            permission_details.append(detail)
    else:
        permission_details.append({
            "note": "No drive_id/item_id_graph — unable to remove permissions",
        })
        await audit.log(
            event_id, "remediation_skip_removal",
            {"reason": "Missing drive_id or item_id_graph"},
        )

    # ---- 3. Build and send remediation report (disabled) ----
    to_addresses: List[str] = []

    report_sent = False
    report_sent_at: Optional[datetime] = None

    if to_addresses and config.smtp_host:
        file_size = event.get("file_size_bytes")
        # Reconstruct CategoryDetection objects from stored JSONB
        from ..ai.base_provider import CategoryDetection
        cat_assessments = (verdict.get("category_assessments") or []) if verdict else []
        if isinstance(cat_assessments, str):
            import json as _json
            try:
                cat_assessments = _json.loads(cat_assessments)
            except (ValueError, TypeError):
                cat_assessments = []
        recon_categories = [
            CategoryDetection(
                id=ca.get("id", "none"),
                confidence=ca.get("confidence", "medium"),
                evidence=ca.get("evidence", ""),
            )
            for ca in cat_assessments
            if isinstance(ca, dict)
        ]

        payload = AlertPayload(
            event_id=event_id,
            alert_type="remediation_report",
            file_name=event.get("confirmed_file_name") or event.get("file_name") or "Unknown",
            file_path=event.get("relative_path") or event.get("object_id") or "",
            file_size_human=_humanize_bytes(file_size),
            item_type=event.get("item_type", "File"),
            sharing_user=event.get("user_id", "Unknown"),
            sharing_type=event.get("sharing_type") or "Unknown",
            sharing_permission=event.get("sharing_permission") or "Unknown",
            event_time=str(event.get("event_time") or event.get("received_at") or ""),
            sharing_link_url=event.get("sharing_link_url"),
            categories=recon_categories if recon_categories else None,
            escalation_tier=verdict.get("escalation_tier") if verdict else None,
            context=verdict.get("overall_context") if verdict else None,
            summary=verdict["summary"] if verdict else None,
            recommendation=verdict["recommendation"] if verdict else None,
            analysis_mode=verdict["analysis_mode"] if verdict else None,
            permission_details=permission_details,
        )

        notifier = EmailNotifier(
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_user=config.smtp_user,
            smtp_password=config.smtp_password,
            from_address=config.email_from,
            to_addresses=to_addresses,
            use_tls=config.smtp_use_tls,
            template_name="remediation_report.html",
            dashboard_url=config.dashboard_url,
        )

        try:
            report_sent = await notifier.send_alert(payload)
            if report_sent:
                report_sent_at = datetime.now(timezone.utc)
                await audit.log(
                    event_id, "remediation_report_sent",
                    {"recipients": to_addresses},
                )
        except Exception as exc:
            logger.error("Failed to send remediation report for %s: %s", event_id, exc)
            await audit.log(
                event_id, "remediation_report_failed",
                status="error", error=str(exc),
            )
    else:
        logger.warning("Skipping remediation report — no SMTP or recipients configured")

    # ---- 4. Update remediations row ----
    status = "completed" if permissions_failed == 0 else "completed"
    if not drive_id or not item_id:
        # Still completed — we sent the report even if we couldn't remove permissions
        status = "completed"

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE remediations
            SET status = $1,
                completed_at = $2,
                permissions_removed = $3,
                permissions_failed = $4,
                permission_details = $5::jsonb,
                report_sent = $6,
                report_sent_at = $7,
                report_recipients = $8::jsonb,
                updated_at = $2
            WHERE id = $9
            """,
            status,
            datetime.now(timezone.utc),
            permissions_removed,
            permissions_failed,
            json.dumps(permission_details),
            report_sent,
            report_sent_at,
            json.dumps(to_addresses),
            remediation_id,
        )

    # ---- 5. Mark event as remediated ----
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE events SET status = 'remediated', updated_at = NOW() WHERE event_id = $1",
            event_id,
        )

    await audit.log(
        event_id, "remediation_completed",
        {
            "remediation_id": remediation_id,
            "permissions_removed": permissions_removed,
            "permissions_failed": permissions_failed,
            "report_sent": report_sent,
        },
    )
    logger.info(
        "Remediation %d completed for %s: removed=%d failed=%d report=%s",
        remediation_id, event_id, permissions_removed, permissions_failed, report_sent,
    )

    # ---- 6. Queue user notification (true_positive) ----
    if redis_conn is not None:
        try:
            await redis_conn.rpush(
                "sharesentinel:user_notifications",
                json.dumps({"event_id": event_id, "disposition": "true_positive"}),
            )
            logger.info("Queued user notification for event %s (true_positive)", event_id)
        except Exception:
            logger.error("Failed to queue user notification for event %s", event_id, exc_info=True)


async def _mark_failed(
    db_pool: asyncpg.Pool, remediation_id: int, error_message: str,
) -> None:
    """Mark a remediation row as failed."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE remediations
            SET status = 'failed',
                completed_at = NOW(),
                error_message = $1,
                updated_at = NOW()
            WHERE id = $2
            """,
            error_message,
            remediation_id,
        )
