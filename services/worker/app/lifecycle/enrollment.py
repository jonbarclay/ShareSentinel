"""Enroll sharing links into the lifecycle tracking table.

Called from the metadata pre-screen after Graph API permissions are fetched.
Each anonymous/org-wide permission gets a row in sharing_link_lifecycle.
Links with a Microsoft-set expirationDateTime are marked 'ms_managed' and
exempt from our countdown notifications and removal.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)

# Graph API sentinel value for "no expiration"
_MS_SENTINEL_DATE = "0001-01-01T00:00:00Z"


async def enroll_sharing_links(
    db_pool: asyncpg.Pool,
    permissions: List[Dict[str, Any]],
    event_id: str,
    user_id: str,
    drive_id: str,
    item_id: str,
    file_name: str,
    event_time: Optional[datetime],
) -> int:
    """Insert lifecycle rows for each anonymous/org-wide sharing permission.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool.
    permissions : list
        Raw permission objects from the Graph API.
    event_id : str
        The event ID this sharing event belongs to.
    user_id : str
        The user who created the sharing link.
    drive_id, item_id : str
        Graph API identifiers for the shared item.
    file_name : str
        Display name of the shared file/folder.
    event_time : datetime or None
        Splunk audit log CreationTime — day zero for the 180-day countdown.

    Returns
    -------
    int
        Number of rows inserted (0 if all were duplicates or no qualifying permissions).
    """
    if not event_time:
        logger.warning(
            "No event_time for event_id=%s — cannot enroll lifecycle rows", event_id,
        )
        return 0

    # Ensure event_time is a datetime (may arrive as ISO string from audit poller)
    if isinstance(event_time, str):
        from datetime import timezone
        event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))

    enrolled = 0

    for perm in permissions:
        link = perm.get("link")
        if not link:
            continue

        scope = link.get("scope", "").lower()
        if scope not in ("anonymous", "organization"):
            continue

        permission_id = perm.get("id")
        if not permission_id:
            continue

        link_type = link.get("type", "view").lower()
        link_url = link.get("webUrl", "")

        # Determine if MS manages the expiration
        ms_expiration = _parse_ms_expiration(perm.get("expirationDateTime"))
        status = "ms_managed" if ms_expiration else "active"

        try:
            async with db_pool.acquire() as conn:
                result = await conn.execute(
                    """
                    INSERT INTO sharing_link_lifecycle (
                        event_id, permission_id, drive_id, item_id, user_id,
                        link_created_at, ms_expiration_at, status,
                        file_name, sharing_scope, sharing_type, link_url
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (event_id, permission_id) DO NOTHING
                    """,
                    event_id,
                    permission_id,
                    drive_id,
                    item_id,
                    user_id,
                    event_time,
                    ms_expiration,
                    status,
                    file_name,
                    scope,
                    link_type,
                    link_url,
                )
                if result == "INSERT 0 1":
                    enrolled += 1
                    logger.debug(
                        "Enrolled lifecycle row: event_id=%s perm=%s status=%s",
                        event_id, permission_id, status,
                    )
        except Exception:
            logger.exception(
                "Failed to enroll lifecycle row event_id=%s perm=%s",
                event_id, permission_id,
            )

    if enrolled:
        logger.info(
            "Enrolled %d sharing link(s) for event_id=%s", enrolled, event_id,
        )
    return enrolled


def _parse_ms_expiration(value: Any) -> Optional[datetime]:
    """Parse the Graph API expirationDateTime, returning None if absent or sentinel."""
    if not value or str(value) == _MS_SENTINEL_DATE:
        return None
    try:
        from datetime import timezone

        if isinstance(value, datetime):
            return value
        # Graph API returns ISO 8601 strings
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt
    except (ValueError, TypeError):
        logger.warning("Unparseable expirationDateTime: %s", value)
        return None
