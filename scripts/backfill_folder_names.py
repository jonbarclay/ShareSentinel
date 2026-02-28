"""Backfill confirmed_file_name for folder events that were processed before
the folder-name fix in orchestrator.py.

Run inside the worker container:
    python -m scripts.backfill_folder_names [--dry-run] [--limit 100]
"""

import argparse
import asyncio
import logging
import sys

import asyncpg

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.client import GraphClient, FileNotFoundError, AccessDeniedError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_folder_names")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill confirmed_file_name for folder events."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be updated without making changes")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max number of folders to process (0 = all)")
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
    graph_client = GraphClient(auth=auth)

    # Find folder events with missing confirmed_file_name
    query = """
        SELECT event_id, object_id, site_url, file_name
        FROM events
        WHERE item_type = 'Folder'
          AND (confirmed_file_name IS NULL OR confirmed_file_name = '')
        ORDER BY created_at
    """
    rows = await db_pool.fetch(query)

    logger.info("Found %d folder events with missing confirmed_file_name", len(rows))

    if args.limit > 0:
        rows = rows[:args.limit]
        logger.info("Limited to %d rows", len(rows))

    if args.dry_run:
        for row in rows:
            logger.info(
                "  [DRY RUN] event_id=%s  file_name=%s  object_id=%s",
                row["event_id"], row["file_name"], row["object_id"],
            )
        logger.info("Dry run — no changes made")
        await db_pool.close()
        return

    updated = 0
    skipped = 0
    errors = 0

    for i, row in enumerate(rows):
        event_id = row["event_id"]
        object_id = row["object_id"] or ""

        if not object_id:
            logger.warning("No object_id for event %s, skipping", event_id)
            skipped += 1
            continue

        try:
            metadata = await graph_client.get_item_metadata(object_id)
            folder_name = metadata.get("name", "")

            if not folder_name:
                logger.warning("No name in metadata for event %s, skipping", event_id)
                skipped += 1
                continue

            await db_pool.execute(
                "UPDATE events SET confirmed_file_name = $1 WHERE event_id = $2",
                folder_name, event_id,
            )
            updated += 1
            logger.info(
                "Updated event %s: confirmed_file_name = %s",
                event_id, folder_name,
            )

        except (FileNotFoundError, AccessDeniedError) as exc:
            skipped += 1
            logger.warning(
                "Cannot access folder for event %s (%s): %s",
                event_id, type(exc).__name__, exc,
            )

        except Exception:
            errors += 1
            logger.warning(
                "Failed to backfill event_id=%s", event_id, exc_info=True,
            )

        # Rate limit Graph API calls
        await asyncio.sleep(0.2)

        if (i + 1) % 25 == 0:
            logger.info(
                "Progress: %d/%d processed, %d updated, %d skipped, %d errors",
                i + 1, len(rows), updated, skipped, errors,
            )

    logger.info(
        "Backfill complete: %d processed, %d updated, %d skipped, %d errors",
        len(rows), updated, skipped, errors,
    )

    await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
