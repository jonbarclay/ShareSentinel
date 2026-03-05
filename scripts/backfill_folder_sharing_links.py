"""Backfill sharing_links for folder events that are missing them.

Many folder events were processed without their sharing permissions being
fetched/stored on the event row. This script resolves each folder's metadata
via the Graph API /shares endpoint, fetches sharing permissions, and updates
both the events table and sharing_link_lifecycle table.

Run inside the worker container:
    python -m scripts.backfill_folder_sharing_links [--dry-run] [--batch-size 20]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
from typing import Any

import asyncpg
import httpx

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.sharing import (
    extract_all_sharing_links,
    extract_sharing_link,
    get_sharing_permissions,
)
from app.lifecycle.enrollment import enroll_sharing_links

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_folder_sharing_links")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
API_DELAY = 0.3  # seconds between Graph API calls


def _encode_sharing_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode()
    return "u!" + encoded.rstrip("=")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()

    config = Config.from_env()
    db_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=5)
    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )

    # Find folder events missing sharing links
    rows = await db_pool.fetch("""
        SELECT e.event_id, e.file_name, e.object_id, e.user_id, e.event_time,
               e.sharing_type, e.sharing_scope
        FROM events e
        WHERE e.item_type = 'Folder'
          AND e.parent_event_id IS NULL
          AND e.status = 'completed'
          AND e.sharing_links IS NULL
          AND e.object_id IS NOT NULL AND e.object_id != ''
        ORDER BY e.received_at DESC
    """)

    logger.info("Found %d folder events with missing sharing links", len(rows))

    if args.dry_run:
        for r in rows[:10]:
            logger.info("  [DRY RUN] %s — %s", r["event_id"], r["file_name"])
        if len(rows) > 10:
            logger.info("  ... and %d more", len(rows) - 10)
        await db_pool.close()
        return

    updated = 0
    lifecycle_enrolled = 0
    skipped = 0
    errors = 0

    for i, row in enumerate(rows):
        event_id = row["event_id"]
        object_id = row["object_id"]

        try:
            # Step 1: Resolve folder via /shares endpoint
            share_token = _encode_sharing_url(object_id)
            url = f"{GRAPH_BASE}/shares/{share_token}/driveItem"
            headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers)

            if resp.status_code != 200:
                logger.warning(
                    "  [%d/%d] %s: /shares returned %d — skipping",
                    i + 1, len(rows), event_id, resp.status_code,
                )
                skipped += 1
                await asyncio.sleep(API_DELAY)
                continue

            folder_meta = resp.json()
            parent_ref = folder_meta.get("parentReference") or {}
            drive_id = parent_ref.get("driveId", "")
            item_id = folder_meta.get("id", "")
            web_url = folder_meta.get("webUrl", "")
            name = folder_meta.get("name", row["file_name"] or "")

            if not drive_id or not item_id:
                logger.warning(
                    "  [%d/%d] %s: missing drive_id/item_id — skipping",
                    i + 1, len(rows), event_id,
                )
                skipped += 1
                await asyncio.sleep(API_DELAY)
                continue

            # Step 2: Fetch sharing permissions
            permissions = await get_sharing_permissions(auth, drive_id, item_id)
            sharing_link_url = extract_sharing_link(permissions)
            sharing_links = extract_all_sharing_links(permissions)

            # Fix labels to use spaced format
            for sl in sharing_links:
                scope = sl.get("scope", "")
                link_type = sl.get("type", "view")
                sl["label"] = scope.capitalize() + " " + link_type.capitalize()

            # Step 3: Update the event record
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """UPDATE events SET
                        drive_id = $1, item_id_graph = $2, web_url = $3,
                        sharing_link_url = $4, sharing_links = $5::jsonb,
                        confirmed_file_name = COALESCE(confirmed_file_name, $6),
                        updated_at = NOW()
                    WHERE event_id = $7""",
                    drive_id, item_id, web_url,
                    sharing_link_url,
                    json.dumps(sharing_links) if sharing_links else None,
                    name,
                    event_id,
                )
            updated += 1

            # Step 4: Enroll in lifecycle tracking
            if sharing_links and row["event_time"]:
                try:
                    count = await enroll_sharing_links(
                        db_pool=db_pool,
                        permissions=permissions,
                        event_id=event_id,
                        user_id=row["user_id"] or "",
                        drive_id=drive_id,
                        item_id=item_id,
                        file_name=name,
                        event_time=row["event_time"],
                    )
                    lifecycle_enrolled += count
                except Exception:
                    logger.warning(
                        "  Lifecycle enrollment failed for %s", event_id,
                        exc_info=True,
                    )

            logger.info(
                "  [%d/%d] %s (%s): %d sharing links found, link_url=%s",
                i + 1, len(rows), event_id, name,
                len(sharing_links),
                sharing_link_url[:60] + "..." if sharing_link_url else "none",
            )

        except Exception:
            errors += 1
            logger.warning(
                "  [%d/%d] %s: error — %s", i + 1, len(rows), event_id,
                "see traceback", exc_info=True,
            )

        if (i + 1) % args.batch_size == 0:
            logger.info(
                "Progress: %d/%d processed, %d updated, %d skipped, %d errors",
                i + 1, len(rows), updated, skipped, errors,
            )

        await asyncio.sleep(API_DELAY)

    logger.info(
        "Backfill complete: %d processed, %d updated, %d lifecycle enrolled, "
        "%d skipped, %d errors",
        len(rows), updated, lifecycle_enrolled, skipped, errors,
    )
    await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
