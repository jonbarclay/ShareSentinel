"""Admin API endpoints for system configuration and user management."""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..auth import require_role

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


# --- Request models ---

class SettingUpdate(BaseModel):
    key: str
    value: str


class UpdateSettingsRequest(BaseModel):
    settings: list[SettingUpdate]


# --- Settings ---

@router.get("/admin/settings")
async def get_settings(request: Request, user=require_role("admin")):
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, value, description, category, data_type, display_name,
                   updated_by, updated_at
            FROM configuration
            WHERE category IS NOT NULL
            ORDER BY category, key
            """
        )

    categories: dict[str, list] = defaultdict(list)
    for r in rows:
        categories[r["category"]].append(dict(r))

    return {"categories": dict(categories)}


@router.patch("/admin/settings")
async def update_settings(request: Request, body: UpdateSettingsRequest, user=require_role("admin")):
    pool = _pool(request)
    admin_email = user.get("email", "unknown") if isinstance(user, dict) else "unknown"
    updated = []
    errors = []

    async with pool.acquire() as conn:
        for item in body.settings:
            # Validate key exists
            row = await conn.fetchrow(
                "SELECT key, value, data_type FROM configuration WHERE key = $1",
                item.key,
            )
            if not row:
                errors.append({"key": item.key, "error": "Unknown setting key"})
                continue

            # Validate data_type
            data_type = row["data_type"]
            if data_type == "int":
                try:
                    int(item.value) if item.value else None
                except ValueError:
                    errors.append({"key": item.key, "error": "Value must be an integer"})
                    continue
            elif data_type == "float":
                try:
                    float(item.value) if item.value else None
                except ValueError:
                    errors.append({"key": item.key, "error": "Value must be a float"})
                    continue
            elif data_type == "boolean":
                if item.value not in ("true", "false", ""):
                    errors.append({"key": item.key, "error": "Value must be 'true', 'false', or empty"})
                    continue

            old_value = row["value"]

            # Update the setting
            await conn.execute(
                """
                UPDATE configuration
                SET value = $1, updated_by = $2, updated_at = NOW()
                WHERE key = $3
                """,
                item.value,
                admin_email,
                item.key,
            )

            # Audit log
            await conn.execute(
                """
                INSERT INTO audit_log (action, details, status)
                VALUES ('config_updated', $1::jsonb, 'info')
                """,
                json.dumps({
                    "key": item.key,
                    "old_value": old_value,
                    "new_value": item.value,
                    "updated_by": admin_email,
                }),
            )

            updated.append(item.key)

    return {"updated": updated, "errors": errors}


# --- Users ---

@router.get("/admin/users")
async def list_users(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    user=require_role("admin"),
):
    pool = _pool(request)
    offset = (page - 1) * per_page

    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM dashboard_users"
        )
        rows = await conn.fetch(
            """
            SELECT id, oid, email, display_name, groups, roles,
                   first_seen_at, last_seen_at
            FROM dashboard_users
            ORDER BY last_seen_at DESC
            LIMIT $1 OFFSET $2
            """,
            per_page, offset,
        )

    return {
        "total": count_row["total"],
        "users": [dict(r) for r in rows],
    }
