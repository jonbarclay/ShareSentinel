"""Backfill sharing_link_lifecycle for events that were processed before the
datetime string fix in enrollment.py.

Run inside the worker container:
    python -m scripts.backfill_lifecycle_enrollment [--dry-run] [--batch-size 50]
"""

import argparse
import asyncio
import logging
import sys

import asyncpg

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.sharing import get_sharing_permissions
from app.lifecycle.enrollment import enroll_sharing_links

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_lifecycle")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
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

    # Find events missing lifecycle rows
    rows = await db_pool.fetch("""
        SELECT e.event_id, e.user_id, e.drive_id, e.item_id_graph, e.file_name, e.event_time
        FROM events e
        WHERE e.status = 'completed'
          AND e.operation IN ('CompanyLinkCreated', 'CompanySharingLinkCreated', 'AnonymousLinkCreated')
          AND e.drive_id IS NOT NULL AND e.drive_id != ''
          AND e.item_id_graph IS NOT NULL AND e.item_id_graph != ''
          AND e.event_id NOT IN (SELECT event_id FROM sharing_link_lifecycle)
        ORDER BY e.created_at
    """)

    logger.info("Found %d events missing lifecycle enrollment", len(rows))

    if args.dry_run:
        logger.info("Dry run — exiting")
        await db_pool.close()
        return

    enrolled_total = 0
    errors = 0

    for i, row in enumerate(rows):
        try:
            permissions = await get_sharing_permissions(
                auth=auth,
                drive_id=row["drive_id"],
                item_id=row["item_id_graph"],
            )

            count = await enroll_sharing_links(
                db_pool=db_pool,
                permissions=permissions,
                event_id=row["event_id"],
                user_id=row["user_id"],
                drive_id=row["drive_id"],
                item_id=row["item_id_graph"],
                file_name=row["file_name"] or "",
                event_time=row["event_time"],
            )
            enrolled_total += count

            if (i + 1) % args.batch_size == 0:
                logger.info(
                    "Progress: %d/%d processed, %d enrolled, %d errors",
                    i + 1, len(rows), enrolled_total, errors,
                )

            # Small delay to avoid Graph API rate limits
            await asyncio.sleep(0.2)

        except Exception:
            errors += 1
            logger.warning(
                "Failed to backfill event_id=%s", row["event_id"], exc_info=True,
            )

    logger.info(
        "Backfill complete: %d processed, %d enrolled, %d errors",
        len(rows), enrolled_total, errors,
    )

    await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
