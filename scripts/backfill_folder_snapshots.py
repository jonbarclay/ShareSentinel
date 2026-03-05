"""Pre-populate folder content snapshots via Graph API enumeration.

Run once via docker exec before enabling folder rescan to avoid an initial
burst of worker re-downloads on first rescan cycle:

    docker exec sharesentinel-lifecycle-cron python -m scripts.backfill_folder_snapshots --dry-run
    docker exec sharesentinel-lifecycle-cron python -m scripts.backfill_folder_snapshots

Without pre-population, the first rescan treats all files as "new" since there
is no snapshot to diff against. The worker's file hash dedup will still prevent
redundant AI analysis, but this avoids unnecessary downloads.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main(dry_run: bool = False) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    db_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)

    # Import Graph API auth
    try:
        from app.graph_api import GraphAuth, enumerate_folder_children
    except ImportError:
        logger.error("Cannot import graph_api — run this inside the lifecycle-cron container")
        sys.exit(1)

    auth = GraphAuth(
        tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
        client_id=os.environ.get("AZURE_CLIENT_ID", ""),
        client_secret=os.environ.get("AZURE_CLIENT_SECRET", ""),
        certificate_path=os.environ.get("AZURE_CERTIFICATE") or None,
        certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS") or None,
    )

    # Find folder events with active lifecycle links but no snapshot
    async with db_pool.acquire() as conn:
        folders = await conn.fetch(
            """
            SELECT DISTINCT
                sll.event_id AS parent_event_id,
                COALESCE(NULLIF(sll.drive_id, ''), e.drive_id) AS folder_drive_id,
                COALESCE(NULLIF(sll.item_id, ''), e.item_id_graph) AS folder_item_id,
                COALESCE(NULLIF(sll.file_name, ''), e.file_name) AS folder_name
            FROM sharing_link_lifecycle sll
            JOIN events e ON e.event_id = sll.event_id
            WHERE sll.status = 'active'
              AND e.item_type = 'Folder'
              AND NOT EXISTS (
                  SELECT 1 FROM folder_content_snapshots fcs
                  WHERE fcs.parent_event_id = sll.event_id
              )
            """
        )

    if not folders:
        logger.info("No folders need backfilling")
        await db_pool.close()
        return

    logger.info("Found %d folders to backfill", len(folders))
    now = datetime.now(timezone.utc)
    total_files = 0

    for folder in folders:
        parent_event_id = folder["parent_event_id"]
        drive_id = folder["folder_drive_id"]
        item_id = folder["folder_item_id"]
        folder_name = folder["folder_name"]

        logger.info("Backfilling folder: %s (%s)", folder_name, parent_event_id[:12])

        if dry_run:
            logger.info("  [DRY RUN] Would enumerate and store snapshot")
            continue

        try:
            children = await enumerate_folder_children(auth, drive_id, item_id)
        except Exception:
            logger.exception("  Failed to enumerate folder %s", parent_event_id[:12])
            continue

        logger.info("  Enumerated %d files", len(children))
        total_files += len(children)

        async with db_pool.acquire() as conn:
            for child in children:
                parent_ref = child.get("parentReference", {})
                await conn.execute(
                    """
                    INSERT INTO folder_content_snapshots (
                        parent_event_id, folder_drive_id, folder_item_id,
                        child_item_id, child_name, child_size, child_mime_type,
                        child_web_url, child_parent_path, child_ctag, child_etag,
                        first_seen_at, last_seen_at, last_scanned_at, times_scanned
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12, $12, 1)
                    ON CONFLICT (parent_event_id, child_item_id) DO NOTHING
                    """,
                    parent_event_id,
                    drive_id,
                    item_id,
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

            # Create rescan state row
            await conn.execute(
                """
                INSERT INTO folder_rescan_state (
                    parent_event_id, folder_drive_id, folder_item_id,
                    folder_name, total_files
                ) VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (parent_event_id) DO NOTHING
                """,
                parent_event_id,
                drive_id,
                item_id,
                folder_name,
                len(children),
            )

        logger.info("  Stored snapshot for %d files", len(children))

    logger.info("Backfill complete: %d folders, %d total files", len(folders), total_files)
    await db_pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill folder content snapshots")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
