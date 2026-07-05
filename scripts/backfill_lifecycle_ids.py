"""Repair sharing_link_lifecycle rows enrolled with empty drive_id/item_id.

A 2026-02-26 backfill batch created ~590 lifecycle rows with drive_id='' and
item_id='' (and link_url=''). Removal then fails forever with
``400 .../drives//items//permissions/...`` — the daily processor marks them
status='error', and still-active rows from the batch keep joining the error
pool as they cross 180 days. Those links are past expiry and never removed.

Resolution per row (joined to ``events`` on event_id):
1. events.drive_id + events.item_id_graph populated → copy them.
2. Else resolve events.object_id (file URL) via Graph ``/shares/{u!...}/driveItem``.
3. Graph 404 (file deleted) → status='manually_removed' (nothing left to remove).
4. Resolved rows with status='error' are reset to 'active' so the next daily
   lifecycle run removes the (long overdue) permission. A permission that was
   already removed out-of-band returns 404 on DELETE, which the remover treats
   as success.

Run inside the worker container:
    docker exec sharesentinel-worker python -m scripts.backfill_lifecycle_ids            # dry run
    docker exec sharesentinel-worker python -m scripts.backfill_lifecycle_ids --execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import sys

import asyncpg

sys.path.insert(0, "/app")

from app.config import Config  # noqa: E402
from app.graph_api.auth import GraphAuth  # noqa: E402
from app.graph_api.client import (  # noqa: E402
    FileNotFoundError as GraphFileNotFoundError,
    GraphClient,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_lifecycle_ids")

QUERY = """
SELECT l.id, l.event_id, l.status, l.file_name,
       e.drive_id AS ev_drive_id, e.item_id_graph AS ev_item_id,
       e.object_id, e.web_url
FROM sharing_link_lifecycle l
JOIN events e ON e.event_id = l.event_id
WHERE (l.drive_id = '' OR l.item_id = '')
  AND l.status IN ('active', 'error')
ORDER BY l.id
"""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    args = parser.parse_args()
    dry = not args.execute

    config = Config.from_env()
    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )
    client = GraphClient(auth)
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=2)

    rows = await pool.fetch(QUERY)
    if args.limit:
        rows = rows[: args.limit]
    logger.info("%d lifecycle rows need drive/item IDs (dry_run=%s)", len(rows), dry)

    stats = {"from_events": 0, "from_graph": 0, "item_gone": 0, "unresolved": 0}
    for row in rows:
        lid = row["id"]
        drive_id = row["ev_drive_id"] or ""
        item_id = row["ev_item_id"] or ""
        source = "events"

        if not (drive_id and item_id):
            url = row["object_id"] or row["web_url"] or ""
            if not url:
                stats["unresolved"] += 1
                logger.warning("id=%d: no object_id/web_url to resolve — skipping", lid)
                continue
            try:
                meta = await client.get_item_metadata(url)
                drive_id = (meta.get("parentReference") or {}).get("driveId", "")
                item_id = meta.get("id", "")
                source = "graph"
            except GraphFileNotFoundError:
                stats["item_gone"] += 1
                logger.info("id=%d: item gone (404) — marking manually_removed", lid)
                if not dry:
                    await pool.execute(
                        """
                        UPDATE sharing_link_lifecycle
                        SET status = 'manually_removed',
                            removal_error = 'item_no_longer_exists (backfill_lifecycle_ids)',
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        lid,
                    )
                continue
            except Exception as exc:
                stats["unresolved"] += 1
                logger.warning("id=%d: resolution failed: %s", lid, str(exc)[:160])
                continue

        if not (drive_id and item_id):
            stats["unresolved"] += 1
            logger.warning("id=%d: resolved empty ids (source=%s) — skipping", lid, source)
            continue

        stats["from_events" if source == "events" else "from_graph"] += 1
        logger.info(
            "id=%d: drive=%s item=%s (via %s)%s",
            lid, drive_id[:20], item_id[:20], source,
            " [dry-run]" if dry else "",
        )
        if not dry:
            await pool.execute(
                """
                UPDATE sharing_link_lifecycle
                SET drive_id = $1,
                    item_id = $2,
                    status = 'active',
                    removal_error = NULL,
                    updated_at = NOW()
                WHERE id = $3
                """,
                drive_id, item_id, lid,
            )
        await asyncio.sleep(0.1)  # be polite to Graph

    logger.info("Done: %s (dry_run=%s)", stats, dry)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
