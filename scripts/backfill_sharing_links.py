"""Backfill sharing_links JSONB column for existing events.

Run inside the worker container:
    python -m scripts.backfill_sharing_links
"""

import asyncio
import json
import logging
import os
import sys

import asyncpg
import httpx

# Add parent so we can import from app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.graph_api.auth import GraphAuth
from app.graph_api.sharing import extract_all_sharing_links, get_sharing_permissions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL", "postgresql://sharesentinel:sharesentinel@postgres:5432/sharesentinel")
    pool = await asyncpg.create_pool(db_url)

    auth = GraphAuth(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
        certificate_path=os.environ.get("AZURE_CERTIFICATE"),
        certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS") or os.environ.get("AZURE_CERTIFICATE_PASSWORD"),
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_id, drive_id, item_id_graph
            FROM events
            WHERE drive_id IS NOT NULL AND drive_id <> ''
              AND item_id_graph IS NOT NULL AND item_id_graph <> ''
              AND sharing_links IS NULL
            ORDER BY received_at
            """
        )

    logger.info("Found %d events to backfill", len(rows))
    success = 0
    failed = 0

    for row in rows:
        event_id = row["event_id"]
        drive_id = row["drive_id"]
        item_id = row["item_id_graph"]
        try:
            permissions = await get_sharing_permissions(auth, drive_id, item_id)
            links = extract_all_sharing_links(permissions)
            if links:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE events SET sharing_links = $1::jsonb WHERE event_id = $2",
                        json.dumps(links),
                        event_id,
                    )
                logger.info("[%s] Backfilled %d sharing link(s)", event_id, len(links))
            else:
                # Store empty array so we don't re-process
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE events SET sharing_links = '[]'::jsonb WHERE event_id = $1",
                        event_id,
                    )
                logger.info("[%s] No sharing links found, set to []", event_id)
            success += 1
        except httpx.HTTPStatusError as exc:
            logger.warning("[%s] HTTP %s — %s", event_id, exc.response.status_code, exc.response.text[:200])
            failed += 1
        except Exception:
            logger.exception("[%s] Failed to backfill", event_id)
            failed += 1

    await pool.close()
    logger.info("Backfill complete: %d succeeded, %d failed out of %d", success, failed, len(rows))


if __name__ == "__main__":
    asyncio.run(main())
