"""Weekly folder rescan: detect new/modified files in shared folders via cTag comparison.

Runs as a loop in the lifecycle-cron container. For each folder with an active
sharing link, re-enumerates contents via Graph API and compares cTags against
the stored snapshot. Only new or modified files are enqueued to the worker
for AI analysis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from .config import LifecycleConfig
from .graph_api import GraphAuth, enumerate_folder_children, get_item_permissions

logger = logging.getLogger(__name__)

QUEUE_KEY = "sharesentinel:jobs"
DEDUP_KEY_PREFIX = "sharesentinel:dedup:"
DEDUP_TTL_SECONDS = 86400  # 24 hours


async def run_folder_rescan(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    redis_client: aioredis.Redis,
    config: LifecycleConfig,
) -> dict[str, int]:
    """Run one folder rescan cycle. Returns stats dict."""
    stats = {
        "folders_checked": 0,
        "new_files": 0,
        "modified_files": 0,
        "unchanged_files": 0,
        "deleted_files": 0,
        "links_removed": 0,
        "errors": 0,
    }

    # Find folders due for rescan
    due_folders = await _find_due_folders(db_pool, config)
    if not due_folders:
        logger.info("No folders due for rescan")
        return stats

    logger.info("Found %d folders due for rescan", len(due_folders))
    run_id = uuid.uuid4().hex[:12]

    for folder in due_folders:
        try:
            folder_stats = await _rescan_folder(
                db_pool, auth, redis_client, folder, run_id,
            )
            stats["folders_checked"] += 1
            stats["new_files"] += folder_stats["new"]
            stats["modified_files"] += folder_stats["modified"]
            stats["unchanged_files"] += folder_stats["unchanged"]
            stats["deleted_files"] += folder_stats["deleted"]
            stats["links_removed"] += folder_stats["link_removed"]
        except Exception:
            logger.exception(
                "Error rescanning folder event=%s",
                folder["parent_event_id"],
            )
            stats["errors"] += 1
            await _update_rescan_state(
                db_pool, folder["parent_event_id"], "failed", 0, 0, 0,
            )

        # Throttle between folders
        await asyncio.sleep(2)

    return stats


async def _find_due_folders(
    db_pool: asyncpg.Pool,
    config: LifecycleConfig,
) -> list[dict[str, Any]]:
    """Query folder_rescan_state joined with sharing_link_lifecycle for due folders.

    Also performs lazy backfill: if a folder has active sharing links but no
    rescan_state row, creates one so it gets picked up.
    """
    interval_hours = config.folder_rescan_interval_hours
    batch_size = config.folder_rescan_batch_size

    async with db_pool.acquire() as conn:
        # Lazy backfill: find folder events with active lifecycle links but no rescan state.
        # Uses COALESCE to fall back to events table when lifecycle rows lack drive/item IDs.
        # Also skips folders whose physical location (drive_id + item_id) is already
        # excluded_too_large under a different event_id, preventing re-enqueue of
        # massive folders that were shared via multiple sharing events.
        await conn.execute(
            """
            INSERT INTO folder_rescan_state
                (parent_event_id, folder_drive_id, folder_item_id, folder_name)
            SELECT DISTINCT ON (sll.event_id)
                sll.event_id,
                COALESCE(NULLIF(sll.drive_id, ''), e.drive_id),
                COALESCE(NULLIF(sll.item_id, ''), e.item_id_graph),
                COALESCE(NULLIF(sll.file_name, ''), e.file_name)
            FROM sharing_link_lifecycle sll
            JOIN events e ON e.event_id = sll.event_id
            WHERE sll.status = 'active'
              AND e.item_type = 'Folder'
              AND NOT EXISTS (
                  SELECT 1 FROM folder_rescan_state frs
                  WHERE frs.parent_event_id = sll.event_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM folder_rescan_state frs2
                  WHERE frs2.folder_drive_id = COALESCE(NULLIF(sll.drive_id, ''), e.drive_id)
                    AND frs2.folder_item_id = COALESCE(NULLIF(sll.item_id, ''), e.item_id_graph)
                    AND frs2.last_rescan_status = 'excluded_too_large'
              )
            ON CONFLICT (parent_event_id) DO NOTHING
            """,
        )

        # Repair existing rows that have empty drive/item IDs from earlier backfills.
        # Only update when the events table actually has the data (skip no_metadata rows).
        await conn.execute(
            """
            UPDATE folder_rescan_state frs SET
                folder_drive_id = COALESCE(NULLIF(frs.folder_drive_id, ''), e.drive_id),
                folder_item_id  = COALESCE(NULLIF(frs.folder_item_id, ''), e.item_id_graph),
                folder_name     = COALESCE(NULLIF(frs.folder_name, ''), e.file_name)
            FROM events e
            WHERE e.event_id = frs.parent_event_id
              AND (frs.folder_drive_id = '' OR frs.folder_drive_id IS NULL
                   OR frs.folder_item_id = '' OR frs.folder_item_id IS NULL)
              AND e.drive_id IS NOT NULL AND e.drive_id <> ''
              AND e.item_id_graph IS NOT NULL AND e.item_id_graph <> ''
            """,
        )

        # Find folders due for rescan.
        # Also excludes folders whose physical location (drive_id + item_id) is
        # excluded_too_large under any event_id, not just this one.
        rows = await conn.fetch(
            """
            SELECT
                frs.parent_event_id,
                frs.folder_drive_id,
                frs.folder_item_id,
                frs.folder_name,
                e.user_id,
                e.sharing_type,
                e.sharing_scope,
                e.sharing_permission,
                e.event_time,
                e.object_id
            FROM folder_rescan_state frs
            JOIN events e ON e.event_id = frs.parent_event_id
            WHERE frs.last_rescan_status NOT IN ('link_removed', 'folder_deleted', 'no_metadata', 'excluded_too_large')
              AND (
                  frs.last_rescan_at IS NULL
                  OR frs.last_rescan_at < NOW() - make_interval(hours => $1)
              )
              AND EXISTS (
                  SELECT 1 FROM sharing_link_lifecycle sll
                  WHERE sll.event_id = frs.parent_event_id
                    AND sll.status = 'active'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM folder_rescan_state frs2
                  WHERE frs2.folder_drive_id = frs.folder_drive_id
                    AND frs2.folder_item_id = frs.folder_item_id
                    AND frs2.last_rescan_status = 'excluded_too_large'
                    AND frs2.parent_event_id <> frs.parent_event_id
              )
            ORDER BY frs.last_rescan_at ASC NULLS FIRST
            LIMIT $2
            """,
            interval_hours,
            batch_size,
        )

    return [dict(r) for r in rows]


async def _rescan_folder(
    db_pool: asyncpg.Pool,
    auth: GraphAuth,
    redis_client: aioredis.Redis,
    folder: dict[str, Any],
    run_id: str,
) -> dict[str, int]:
    """Rescan a single folder. Returns per-folder stats."""
    parent_event_id = folder["parent_event_id"]
    drive_id = folder["folder_drive_id"]
    item_id = folder["folder_item_id"]
    folder_name = folder.get("folder_name", "")

    result = {"new": 0, "modified": 0, "unchanged": 0, "deleted": 0, "link_removed": 0}

    # Step 1: Validate sharing link still active
    try:
        permissions = await get_item_permissions(auth, drive_id, item_id)
    except Exception as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            logger.info(
                "[rescan] Folder deleted: event=%s name=%s",
                parent_event_id, folder_name,
            )
            await _update_rescan_state(db_pool, parent_event_id, "folder_deleted", 0, 0, 0)
            return result
        raise

    has_broad_link = any(
        (p.get("link", {}).get("scope", "").lower() in ("anonymous", "organization"))
        for p in permissions
    )
    if not has_broad_link:
        logger.info(
            "[rescan] No active anonymous/org links for folder event=%s — marking link_removed",
            parent_event_id,
        )
        await _update_rescan_state(db_pool, parent_event_id, "link_removed", 0, 0, 0)
        result["link_removed"] = 1
        return result

    # Step 2: Enumerate folder contents
    try:
        current_children = await enumerate_folder_children(auth, drive_id, item_id)
    except Exception as exc:
        logger.warning(
            "[rescan] Enumeration failed for event=%s: %s", parent_event_id, exc,
        )
        raise

    # Step 3: Load existing snapshot
    async with db_pool.acquire() as conn:
        snapshot_rows = await conn.fetch(
            """
            SELECT child_item_id, child_ctag, child_name
            FROM folder_content_snapshots
            WHERE parent_event_id = $1 AND deleted_at IS NULL
            """,
            parent_event_id,
        )

    snapshot_map: dict[str, dict] = {
        r["child_item_id"]: {"ctag": r["child_ctag"], "name": r["child_name"]}
        for r in snapshot_rows
    }

    # Step 4: Diff current vs snapshot
    current_ids = set()
    new_files: list[dict] = []
    modified_files: list[dict] = []

    for child in current_children:
        child_id = child.get("id", "")
        child_ctag = child.get("cTag", "")
        current_ids.add(child_id)

        if child_id not in snapshot_map:
            new_files.append(child)
        elif snapshot_map[child_id]["ctag"] != child_ctag:
            modified_files.append(child)

    # Files in snapshot but not in current enumeration → deleted
    deleted_ids = set(snapshot_map.keys()) - current_ids

    result["new"] = len(new_files)
    result["modified"] = len(modified_files)
    result["unchanged"] = len(current_ids) - len(new_files) - len(modified_files)
    result["deleted"] = len(deleted_ids)

    logger.info(
        "[rescan] event=%s folder=%s: %d new, %d modified, %d unchanged, %d deleted (of %d total)",
        parent_event_id, folder_name,
        result["new"], result["modified"], result["unchanged"], result["deleted"],
        len(current_children),
    )

    # Step 5: Enqueue new/modified files to Redis
    enqueued = 0
    for child in new_files + modified_files:
        child_id = child.get("id", "")
        event_id = hashlib.sha256(
            f"rescan:{parent_event_id}:{child_id}:{run_id}".encode()
        ).hexdigest()

        # Redis dedup
        dedup_key = f"{DEDUP_KEY_PREFIX}{event_id}"
        was_set = await redis_client.set(dedup_key, "1", ex=DEDUP_TTL_SECONDS, nx=True)
        if not was_set:
            continue

        parent_ref = child.get("parentReference", {})
        job = {
            "event_id": event_id,
            "operation": "FolderRescan",
            "workload": folder.get("workload", "SharePoint"),
            "user_id": folder.get("user_id", ""),
            "object_id": child.get("webUrl", "") or folder.get("object_id", ""),
            "site_url": None,
            "file_name": child.get("name", ""),
            "relative_path": parent_ref.get("path", ""),
            "item_type": "File",
            "sharing_type": folder.get("sharing_type", ""),
            "sharing_scope": folder.get("sharing_scope", ""),
            "sharing_permission": folder.get("sharing_permission", ""),
            "event_time": folder.get("event_time", ""),
            "rescan_parent_event_id": parent_event_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }

        await redis_client.rpush(QUEUE_KEY, json.dumps(job, default=str))
        enqueued += 1

    if enqueued:
        logger.info(
            "[rescan] Enqueued %d files for event=%s", enqueued, parent_event_id,
        )

    # Step 6: Update snapshot
    await _update_snapshot(db_pool, parent_event_id, drive_id, item_id, current_children, deleted_ids)

    # Step 7: Update rescan state
    await _update_rescan_state(
        db_pool, parent_event_id, "success",
        result["new"], result["modified"], len(current_children),
    )

    return result


async def _update_snapshot(
    db_pool: asyncpg.Pool,
    parent_event_id: str,
    folder_drive_id: str,
    folder_item_id: str,
    current_children: list[dict],
    deleted_ids: set[str],
) -> None:
    """Upsert snapshot rows for current children and mark deletions."""
    now = datetime.now(timezone.utc)

    async with db_pool.acquire() as conn:
        # Mark deleted files
        if deleted_ids:
            await conn.execute(
                """
                UPDATE folder_content_snapshots
                SET deleted_at = $1
                WHERE parent_event_id = $2
                  AND child_item_id = ANY($3::text[])
                  AND deleted_at IS NULL
                """,
                now,
                parent_event_id,
                list(deleted_ids),
            )

        # Upsert current children
        for child in current_children:
            parent_ref = child.get("parentReference", {})
            await conn.execute(
                """
                INSERT INTO folder_content_snapshots (
                    parent_event_id, folder_drive_id, folder_item_id,
                    child_item_id, child_name, child_size, child_mime_type,
                    child_web_url, child_parent_path, child_ctag, child_etag,
                    first_seen_at, last_seen_at, last_scanned_at, times_scanned
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12, $12, 1)
                ON CONFLICT (parent_event_id, child_item_id) DO UPDATE SET
                    child_name = EXCLUDED.child_name,
                    child_size = EXCLUDED.child_size,
                    child_mime_type = EXCLUDED.child_mime_type,
                    child_web_url = EXCLUDED.child_web_url,
                    child_parent_path = EXCLUDED.child_parent_path,
                    child_ctag = EXCLUDED.child_ctag,
                    child_etag = EXCLUDED.child_etag,
                    last_seen_at = EXCLUDED.last_seen_at,
                    last_scanned_at = EXCLUDED.last_scanned_at,
                    times_scanned = folder_content_snapshots.times_scanned + 1,
                    deleted_at = NULL
                """,
                parent_event_id,
                folder_drive_id,
                folder_item_id,
                child.get("id", ""),
                child.get("name", ""),
                child.get("size", 0),
                (child.get("file") or {}).get("mimeType", ""),
                child.get("webUrl", ""),
                parent_ref.get("path", ""),
                child.get("cTag", ""),
                child.get("eTag", ""),
                now,
            )


async def _update_rescan_state(
    db_pool: asyncpg.Pool,
    parent_event_id: str,
    status: str,
    new_files: int,
    modified_files: int,
    total_files: int,
) -> None:
    """Update the folder_rescan_state row after a rescan."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE folder_rescan_state SET
                last_rescan_at = NOW(),
                last_rescan_status = $2,
                new_files_found = $3,
                modified_files_found = $4,
                total_files = $5,
                updated_at = NOW()
            WHERE parent_event_id = $1
            """,
            parent_event_id,
            status,
            new_files,
            modified_files,
            total_files,
        )
