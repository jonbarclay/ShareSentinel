"""Backfill user_profiles for all distinct user_ids in the events table.

Run inside the worker container:
    python -m scripts.backfill_user_profiles
    python -m scripts.backfill_user_profiles --dry-run
    python -m scripts.backfill_user_profiles --photos-only

Set UPN_DOMAIN env var if user_ids are not already UPNs (required, no default).
"""

import asyncio
import base64
import io
import logging
import os
import sys

import asyncpg
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.graph_api.auth import GraphAuth
from app.graph_api.client import GraphClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def to_upn(user_id: str, domain: str) -> str:
    """Convert a bare user_id to a UPN if it doesn't already contain '@'."""
    if "@" in user_id:
        return user_id
    return f"{user_id}@{domain}"


async def backfill_photos(pool: asyncpg.Pool, client: GraphClient, domain: str) -> None:
    """Fetch photos only for profiles that are missing them."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM user_profiles WHERE photo_base64 IS NULL ORDER BY user_id"
        )

    user_ids = [r["user_id"] for r in rows]
    logger.info("Found %d profiles without photos", len(user_ids))

    success = 0
    skipped = 0
    failed = 0

    for uid in user_ids:
        upn = to_upn(uid, domain)
        try:
            photo_bytes = await client.get_user_photo(upn)
            if photo_bytes:
                img = Image.open(io.BytesIO(photo_bytes))
                img = img.convert("RGB")
                img.thumbnail((96, 96))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                photo_b64 = base64.b64encode(buf.getvalue()).decode()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE user_profiles SET photo_base64 = $1, updated_at = NOW() WHERE user_id = $2",
                        photo_b64,
                        uid,
                    )
                logger.info("[%s] Photo saved (%d bytes)", uid, len(photo_b64))
                success += 1
            else:
                logger.info("[%s] No photo available", uid)
                skipped += 1
        except Exception:
            logger.warning("[%s] Failed to fetch photo (upn=%s)", uid, upn, exc_info=True)
            failed += 1

    logger.info("Photos complete: %d saved, %d no photo, %d failed out of %d", success, skipped, failed, len(user_ids))


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    photos_only = "--photos-only" in sys.argv
    domain = os.environ.get("UPN_DOMAIN", "")

    db_url = os.environ.get("DATABASE_URL", "postgresql://sharesentinel:sharesentinel@postgres:5432/sharesentinel")
    pool = await asyncpg.create_pool(db_url)

    if photos_only:
        auth = GraphAuth(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
            certificate_path=os.environ.get("AZURE_CERTIFICATE"),
            certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS") or os.environ.get("AZURE_CERTIFICATE_PASSWORD"),
        )
        client = GraphClient(auth)
        await backfill_photos(pool, client, domain)
        await pool.close()
        return

    # Get distinct user_ids that don't already have a profile
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT e.user_id
            FROM events e
            LEFT JOIN user_profiles up ON e.user_id = up.user_id
            WHERE up.user_id IS NULL
              AND e.user_id IS NOT NULL AND e.user_id <> ''
            ORDER BY e.user_id
            """
        )

    user_ids = [r["user_id"] for r in rows]
    logger.info("Found %d users without profiles (domain=%s)%s", len(user_ids), domain, " (dry run)" if dry_run else "")

    if dry_run:
        for uid in user_ids:
            logger.info("  Would fetch: %s -> %s", uid, to_upn(uid, domain))
        await pool.close()
        return

    auth = GraphAuth(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
        certificate_path=os.environ.get("AZURE_CERTIFICATE"),
        certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS") or os.environ.get("AZURE_CERTIFICATE_PASSWORD"),
    )
    client = GraphClient(auth)

    success = 0
    failed = 0

    for uid in user_ids:
        upn = to_upn(uid, domain)
        try:
            # Fetch profile
            raw = await client.get_user_profile(upn)
            display_name = raw.get("displayName")
            job_title = raw.get("jobTitle")
            department = raw.get("department")
            mail = raw.get("mail")

            # Fetch manager (graceful 404)
            manager_name = None
            try:
                mgr = await client.get_user_manager(upn)
                manager_name = mgr.get("displayName") if mgr else None
            except Exception:
                logger.debug("No manager for %s", upn)

            # Fetch photo (graceful 404), resize to 96x96
            photo_b64 = None
            try:
                photo_bytes = await client.get_user_photo(upn)
                if photo_bytes:
                    img = Image.open(io.BytesIO(photo_bytes))
                    img = img.convert("RGB")
                    img.thumbnail((96, 96))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    photo_b64 = base64.b64encode(buf.getvalue()).decode()
            except Exception:
                logger.debug("No photo for %s", upn)

            # Upsert keyed on the original user_id (not the UPN)
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_profiles (
                        user_id, display_name, job_title, department,
                        mail, manager_name, photo_base64, fetched_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        job_title = EXCLUDED.job_title,
                        department = EXCLUDED.department,
                        mail = EXCLUDED.mail,
                        manager_name = EXCLUDED.manager_name,
                        photo_base64 = EXCLUDED.photo_base64,
                        updated_at = NOW(),
                        fetched_at = NOW()
                    """,
                    uid, display_name, job_title, department,
                    mail, manager_name, photo_b64,
                )

            logger.info("[%s] %s — %s, %s", uid, display_name, job_title, department)
            success += 1

        except Exception:
            logger.exception("[%s] Failed to fetch profile (upn=%s)", uid, upn)
            failed += 1

    await pool.close()
    logger.info("Backfill complete: %d succeeded, %d failed out of %d", success, failed, len(user_ids))


if __name__ == "__main__":
    asyncio.run(main())
