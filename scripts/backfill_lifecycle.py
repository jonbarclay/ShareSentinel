"""Backfill sharing_link_lifecycle from existing events + CSV creation times.

Usage (inside worker container):
    python -m scripts.backfill_lifecycle /app/config/test_data_with_time.csv

Steps:
1. Load CSV to map UniqueSharingId -> CreationTime
2. Update event_time on matching events in the DB
3. For each event with drive_id/item_id, fetch Graph API permissions
4. Enroll anonymous/org-wide permissions into sharing_link_lifecycle
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.sharing import get_sharing_permissions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

MS_SENTINEL_DATE = "0001-01-01T00:00:00Z"

# Rate limit: pause between Graph API calls (seconds)
API_DELAY = 0.15


def load_csv_times(csv_path: str) -> Dict[str, datetime]:
    """Load CSV and return {UniqueSharingId: CreationTime} mapping."""
    mapping: Dict[str, datetime] = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = row.get("properties.RawEventData.UniqueSharingId", "").strip()
            creation_str = row.get("properties.RawEventData.CreationTime", "").strip()
            if event_id and creation_str:
                try:
                    dt = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
                    mapping[event_id] = dt
                except ValueError:
                    logger.warning("Bad date for %s: %s", event_id, creation_str)
    return mapping


def parse_ms_expiration(value: Any) -> Optional[datetime]:
    if not value or str(value) == MS_SENTINEL_DATE:
        return None
    try:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def main(csv_path: str) -> None:
    config = Config.from_env()
    db_pool = await asyncpg.create_pool(config.database_url, min_size=2, max_size=5)
    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path,
        certificate_password=config.azure_certificate_password,
    )

    # Step 1: Load CSV
    logger.info("Loading CSV from %s", csv_path)
    csv_times = load_csv_times(csv_path)
    logger.info("Loaded %d event_id -> CreationTime mappings", len(csv_times))

    # Step 2: Update event_time for matching events
    updated = 0
    async with db_pool.acquire() as conn:
        for event_id, creation_time in csv_times.items():
            result = await conn.execute(
                """
                UPDATE events SET event_time = $1, updated_at = NOW()
                WHERE event_id = $2
                """,
                creation_time,
                event_id,
            )
            if result == "UPDATE 1":
                updated += 1
    logger.info("Updated event_time on %d events", updated)

    # Step 3: Fetch events that need lifecycle enrollment
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.event_id, e.drive_id, e.item_id_graph, e.event_time,
                   e.user_id, e.confirmed_file_name, e.file_name
            FROM events e
            LEFT JOIN sharing_link_lifecycle slc ON slc.event_id = e.event_id
            WHERE e.drive_id IS NOT NULL
              AND e.item_id_graph IS NOT NULL
              AND e.event_time IS NOT NULL
              AND e.event_id NOT LIKE '%%:child:%%'
              AND slc.id IS NULL
            ORDER BY e.event_time
            """
        )
    logger.info("Found %d events to enroll", len(rows))

    # Step 4: For each event, fetch permissions and enroll
    enrolled_total = 0
    errors = 0
    for i, row in enumerate(rows):
        event_id = row["event_id"]
        drive_id = row["drive_id"]
        item_id = row["item_id_graph"]
        event_time = row["event_time"]
        user_id = row["user_id"] or ""
        file_name = row["confirmed_file_name"] or row["file_name"] or ""

        try:
            permissions = await get_sharing_permissions(auth, drive_id, item_id)
        except Exception as exc:
            logger.warning("Graph API failed for %s: %s", event_id, exc)
            errors += 1
            await asyncio.sleep(API_DELAY)
            continue

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
            ms_expiration = parse_ms_expiration(perm.get("expirationDateTime"))
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
                        event_id, permission_id, drive_id, item_id, user_id,
                        event_time, ms_expiration, status,
                        file_name, scope, link_type, link_url,
                    )
                    if result == "INSERT 0 1":
                        enrolled_total += 1
            except Exception as exc:
                logger.warning("Insert failed %s/%s: %s", event_id, permission_id, exc)
                errors += 1

        if (i + 1) % 100 == 0:
            logger.info(
                "Progress: %d/%d events processed, %d enrolled, %d errors",
                i + 1, len(rows), enrolled_total, errors,
            )

        await asyncio.sleep(API_DELAY)

    logger.info(
        "Backfill complete: %d events processed, %d links enrolled, %d errors",
        len(rows), enrolled_total, errors,
    )
    await db_pool.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python -m scripts.backfill_lifecycle <csv_path>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
