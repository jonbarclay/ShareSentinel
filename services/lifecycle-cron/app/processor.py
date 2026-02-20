"""Core lifecycle processor: query milestones, send notifications, remove links."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import asyncpg

from .config import LifecycleConfig
from .graph_api import GraphAuth, remove_sharing_permission
from .notifier import LifecycleNotifier

logger = logging.getLogger(__name__)

# (days_since_creation, db_column, days_remaining)
MILESTONES: List[Tuple[int, str, int]] = [
    (120, "notified_120d_at", 60),
    (150, "notified_150d_at", 30),
    (165, "notified_165d_at", 15),
    (173, "notified_173d_at", 7),
    (178, "notified_178d_at", 2),
    (180, "notified_180d_at", 0),
]


async def process_lifecycle_milestones(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    config: LifecycleConfig,
) -> Dict[str, int]:
    """Run one pass over all active lifecycle rows, processing due milestones.

    Returns a summary dict with counts of notifications sent and links removed.
    """
    notifier = LifecycleNotifier(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_user=config.smtp_user,
        smtp_password=config.smtp_password,
        from_address=config.email_from,
        security_email=config.security_email,
        use_tls=config.smtp_use_tls,
    )

    stats = {"notifications_sent": 0, "links_removed": 0, "errors": 0}

    for days, column, days_remaining in MILESTONES:
        rows = await _fetch_due_rows(db_pool, days, column)
        if not rows:
            continue

        logger.info(
            "Milestone %dd (%s): %d row(s) due", days, column, len(rows),
        )

        for row in rows:
            try:
                if days >= config.max_days:
                    # Removal day
                    await _process_removal(
                        row, db_pool, auth, notifier, column, config,
                    )
                    stats["links_removed"] += 1
                    stats["notifications_sent"] += 1
                else:
                    # Countdown notification
                    await _process_notification(
                        row, db_pool, notifier, column, days_remaining, config,
                    )
                    stats["notifications_sent"] += 1
            except Exception:
                logger.exception(
                    "Error processing lifecycle id=%d milestone=%s",
                    row["id"], column,
                )
                stats["errors"] += 1

    logger.info(
        "Lifecycle pass complete: sent=%d removed=%d errors=%d",
        stats["notifications_sent"], stats["links_removed"], stats["errors"],
    )
    return stats


async def _fetch_due_rows(
    db_pool: asyncpg.Pool, days: int, column: str,
) -> List[Dict[str, Any]]:
    """Find active rows that have passed the milestone but haven't been notified."""
    # column is from our controlled MILESTONES list, not user input
    query = f"""
        SELECT slc.*, up.display_name, up.mail
        FROM sharing_link_lifecycle slc
        LEFT JOIN user_profiles up ON up.user_id = slc.user_id
        WHERE slc.status = 'active'
          AND slc.{column} IS NULL
          AND slc.link_created_at + INTERVAL '{days} days' <= NOW()
        ORDER BY slc.link_created_at
        FOR UPDATE OF slc SKIP LOCKED
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query)
        return [dict(r) for r in rows]


async def _process_notification(
    row: Dict[str, Any],
    db_pool: asyncpg.Pool,
    notifier: LifecycleNotifier,
    column: str,
    days_remaining: int,
    config: LifecycleConfig,
) -> None:
    """Send a countdown notification and record the milestone."""
    user_email = row.get("mail")
    if not user_email:
        logger.warning(
            "No email for user_id=%s lifecycle_id=%d — skipping notification",
            row["user_id"], row["id"],
        )
        # Still mark the milestone so we don't retry every cycle
        await _mark_milestone(db_pool, row["id"], column)
        return

    removal_date = _compute_removal_date(row["link_created_at"], config.max_days)

    sent = await notifier.send_countdown_email(
        to_address=user_email,
        user_display_name=row.get("display_name") or row["user_id"],
        file_name=row.get("file_name") or "Unknown",
        file_path="",  # Not stored on lifecycle row; informational only
        sharing_scope=row.get("sharing_scope") or "",
        sharing_type=row.get("sharing_type") or "",
        link_created_date=row["link_created_at"].strftime("%B %d, %Y"),
        days_remaining=days_remaining,
        removal_date=removal_date.strftime("%B %d, %Y"),
        is_removal_notice=False,
    )

    if sent:
        await _mark_milestone(db_pool, row["id"], column)
    else:
        logger.error(
            "Countdown email failed for lifecycle_id=%d — will retry next cycle",
            row["id"],
        )


async def _process_removal(
    row: Dict[str, Any],
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    notifier: LifecycleNotifier,
    column: str,
    config: LifecycleConfig,
) -> None:
    """Remove the sharing link via Graph API and send removal notification."""
    lifecycle_id = row["id"]
    now = datetime.now(timezone.utc)

    # Attempt removal
    try:
        success = await remove_sharing_permission(
            auth=auth,
            drive_id=row["drive_id"],
            item_id=row["item_id"],
            permission_id=row["permission_id"],
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE sharing_link_lifecycle
                SET status = 'expired_removed',
                    removal_attempted_at = $1,
                    removal_succeeded = $2,
                    updated_at = $1
                WHERE id = $3
                """,
                now, success, lifecycle_id,
            )
        logger.info(
            "Removed sharing permission: lifecycle_id=%d perm=%s",
            lifecycle_id, row["permission_id"],
        )
    except Exception as exc:
        logger.error(
            "Failed to remove permission lifecycle_id=%d: %s", lifecycle_id, exc,
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE sharing_link_lifecycle
                SET status = 'error',
                    removal_attempted_at = $1,
                    removal_succeeded = false,
                    removal_error = $2,
                    updated_at = $1
                WHERE id = $3
                """,
                now, str(exc), lifecycle_id,
            )
        raise

    # Mark the 180d notification milestone
    await _mark_milestone(db_pool, lifecycle_id, column)

    # Send removal notification
    user_email = row.get("mail")
    if user_email:
        removal_date = _compute_removal_date(row["link_created_at"], config.max_days)
        await notifier.send_countdown_email(
            to_address=user_email,
            user_display_name=row.get("display_name") or row["user_id"],
            file_name=row.get("file_name") or "Unknown",
            file_path="",
            sharing_scope=row.get("sharing_scope") or "",
            sharing_type=row.get("sharing_type") or "",
            link_created_date=row["link_created_at"].strftime("%B %d, %Y"),
            days_remaining=0,
            removal_date=removal_date.strftime("%B %d, %Y"),
            is_removal_notice=True,
        )


async def _mark_milestone(
    db_pool: asyncpg.Pool, lifecycle_id: int, column: str,
) -> None:
    """Set a notification milestone timestamp."""
    # column is from our controlled MILESTONES list
    query = f"""
        UPDATE sharing_link_lifecycle
        SET {column} = NOW(), updated_at = NOW()
        WHERE id = $1
    """
    async with db_pool.acquire() as conn:
        await conn.execute(query, lifecycle_id)


def _compute_removal_date(link_created_at: datetime, max_days: int) -> datetime:
    """Calculate the date when the link will be removed."""
    from datetime import timedelta
    return link_created_at + timedelta(days=max_days)
