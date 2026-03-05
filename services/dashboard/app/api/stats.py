"""Statistics API endpoint."""

from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["stats"])

QUEUE_KEY = "sharesentinel:jobs"


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db


@router.get("/stats")
async def get_stats(request: Request):
    pool = _pool(request)
    async with pool.acquire() as conn:
        event_counts = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed
            FROM events
            WHERE parent_event_id IS NULL
        """)
        verdict_stats = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total_verdicts,
                COUNT(*) FILTER (WHERE v.escalation_tier IN ('tier_1', 'tier_2')) AS escalated,
                COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_1') AS tier_1_count,
                COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_2') AS tier_2_count,
                COUNT(*) FILTER (WHERE v.analyst_reviewed) AS reviewed,
                COUNT(*) FILTER (WHERE v.escalation_tier IN ('tier_1', 'tier_2')
                    AND NOT COALESCE(v.analyst_reviewed, FALSE)) AS unreviewed_escalated,
                COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_1'
                    AND NOT COALESCE(v.analyst_reviewed, FALSE)) AS unreviewed_tier_1
            FROM verdicts v
            JOIN events e ON v.event_id = e.event_id
            WHERE e.parent_event_id IS NULL
        """)
        # Total files scanned by AI (including child files from folder processing)
        files_scanned_row = await conn.fetchrow("""
            SELECT COUNT(*) AS total_files_scanned FROM verdicts
        """)
        # Total cost across ALL verdicts (including child events) with second_look
        total_cost_row = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(estimated_cost_usd), 0)::FLOAT
                    + COALESCE(SUM(second_look_cost_usd), 0)::FLOAT AS verdict_cost
            FROM verdicts
        """)
        # User notification AI costs
        notification_cost_row = await conn.fetchrow("""
            SELECT COALESCE(SUM(estimated_cost_usd), 0)::FLOAT AS notification_cost
            FROM user_notifications
        """)
        by_provider = await conn.fetch("""
            SELECT
                ai_provider,
                COUNT(*) AS count,
                (COALESCE(SUM(estimated_cost_usd), 0)
                    + COALESCE(SUM(second_look_cost_usd), 0))::FLOAT AS total_cost,
                AVG(processing_time_seconds)::FLOAT AS avg_latency
            FROM verdicts
            GROUP BY ai_provider
            ORDER BY count DESC
        """)
        by_category = await conn.fetch("""
            SELECT
                cat_elem->>'id' AS category_id,
                COUNT(*) AS count
            FROM verdicts,
                 jsonb_array_elements(category_assessments) AS cat_elem
            WHERE jsonb_typeof(category_assessments) = 'array'
              AND category_assessments != '[]'::jsonb
            GROUP BY cat_elem->>'id'
            ORDER BY count DESC
        """)
        by_tier = await conn.fetch("""
            SELECT escalation_tier, COUNT(*) AS count
            FROM verdicts
            WHERE escalation_tier IS NOT NULL
            GROUP BY escalation_tier
            ORDER BY count DESC
        """)
        top_users = await conn.fetch("""
            SELECT e.user_id,
                   up.display_name,
                   up.department,
                   COUNT(*) AS escalated_count,
                   COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_1') AS tier_1_count,
                   COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_2') AS tier_2_count,
                   MAX(e.event_time) AS latest_event
            FROM events e
            JOIN verdicts v ON v.event_id = e.event_id
            LEFT JOIN user_profiles up ON up.user_id = e.user_id
            WHERE v.escalation_tier IN ('tier_1', 'tier_2')
              AND e.user_id != 'unknown@unknown.com'
              AND e.parent_event_id IS NULL
            GROUP BY e.user_id, up.display_name, up.department
            ORDER BY escalated_count DESC
        """)
        top_sites = await conn.fetch("""
            SELECT e.site_url,
                   COUNT(*) AS escalated_count,
                   COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_1') AS tier_1_count,
                   COUNT(*) FILTER (WHERE v.escalation_tier = 'tier_2') AS tier_2_count,
                   COUNT(DISTINCT e.user_id) AS unique_users,
                   MAX(e.event_time) AS latest_event
            FROM events e
            JOIN verdicts v ON v.event_id = e.event_id
            WHERE v.escalation_tier IN ('tier_1', 'tier_2')
              AND e.site_url IS NOT NULL
              AND e.site_url LIKE '%/sites/%'
              AND e.parent_event_id IS NULL
            GROUP BY e.site_url
            ORDER BY escalated_count DESC
        """)
        recent_escalated = await conn.fetch("""
            SELECT e.event_id, e.file_name, e.user_id,
                   v.escalation_tier, v.category_assessments,
                   v.summary, v.analyst_reviewed, v.created_at
            FROM verdicts v
            JOIN events e ON v.event_id = e.event_id
            WHERE v.escalation_tier IN ('tier_1', 'tier_2')
              AND NOT COALESCE(v.analyst_reviewed, FALSE)
              AND e.parent_event_id IS NULL
            ORDER BY
              CASE v.escalation_tier WHEN 'tier_1' THEN 0 ELSE 1 END,
              v.created_at DESC
            LIMIT 10
        """)

    verdicts_dict = dict(verdict_stats) if verdict_stats else {}
    verdict_cost = total_cost_row["verdict_cost"] if total_cost_row else 0.0
    notification_cost = notification_cost_row["notification_cost"] if notification_cost_row else 0.0
    verdicts_dict["total_cost"] = (verdict_cost or 0.0) + (notification_cost or 0.0)

    # Redis queue depth
    try:
        redis_conn = request.app.state.redis
        queue_depth = await redis_conn.llen(QUEUE_KEY)
    except Exception:
        queue_depth = None

    return {
        "events": dict(event_counts),
        "verdicts": verdicts_dict,
        "total_files_scanned": files_scanned_row["total_files_scanned"] if files_scanned_row else 0,
        "queue_depth": queue_depth,
        "by_provider": [dict(r) for r in by_provider],
        "by_category": [dict(r) for r in by_category],
        "by_tier": [dict(r) for r in by_tier],
        "needs_review": [dict(r) for r in recent_escalated],
        "top_users": [dict(r) for r in top_users],
        "top_sites": [dict(r) for r in top_sites],
    }


@router.get("/stats/scan-activity")
async def get_scan_activity(
    request: Request,
    range: str = Query("30d", pattern=r"^(24h|7d|30d|90d|180d|ytd|all)$"),
):
    """Return daily time-series of files scanned and files flagged (tier 1/2)."""
    pool = _pool(request)

    now = datetime.now(timezone.utc)
    if range == "24h":
        # Hourly buckets for last 24 hours
        trunc = "hour"
        where = "AND v.created_at >= NOW() - INTERVAL '24 hours'"
    elif range == "7d":
        trunc = "day"
        where = "AND v.created_at >= NOW() - INTERVAL '7 days'"
    elif range == "30d":
        trunc = "day"
        where = "AND v.created_at >= NOW() - INTERVAL '30 days'"
    elif range == "90d":
        trunc = "day"
        where = "AND v.created_at >= NOW() - INTERVAL '90 days'"
    elif range == "180d":
        trunc = "day"
        where = "AND v.created_at >= NOW() - INTERVAL '180 days'"
    elif range == "ytd":
        trunc = "day"
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        where = f"AND v.created_at >= '{year_start.isoformat()}'"
    else:  # all
        trunc = "day"
        where = ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT
                date_trunc('{trunc}', v.created_at) AS bucket,
                COUNT(*) AS scanned,
                COUNT(*) FILTER (
                    WHERE v.escalation_tier IN ('tier_1', 'tier_2')
                ) AS flagged
            FROM verdicts v
            WHERE 1=1 {where}
            GROUP BY bucket
            ORDER BY bucket
        """)

    return [
        {
            "date": row["bucket"].isoformat(),
            "scanned": row["scanned"],
            "flagged": row["flagged"],
        }
        for row in rows
    ]
