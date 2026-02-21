"""Statistics API endpoint."""

import asyncpg
from fastapi import APIRouter, Request

router = APIRouter(tags=["stats"])


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
        """)
        verdict_stats = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total_verdicts,
                AVG(sensitivity_rating)::FLOAT AS avg_rating,
                COUNT(*) FILTER (WHERE sensitivity_rating >= 4) AS high_risk,
                COUNT(*) FILTER (WHERE analyst_reviewed) AS reviewed,
                SUM(estimated_cost_usd)::FLOAT AS total_cost
            FROM verdicts
        """)
        by_provider = await conn.fetch("""
            SELECT
                ai_provider,
                COUNT(*) AS count,
                AVG(sensitivity_rating)::FLOAT AS avg_rating,
                SUM(estimated_cost_usd)::FLOAT AS total_cost,
                AVG(processing_time_seconds)::FLOAT AS avg_latency
            FROM verdicts
            GROUP BY ai_provider
            ORDER BY count DESC
        """)
        by_rating = await conn.fetch("""
            SELECT sensitivity_rating, COUNT(*) AS count
            FROM verdicts
            GROUP BY sensitivity_rating
            ORDER BY sensitivity_rating
        """)
        recent_high = await conn.fetch("""
            SELECT e.event_id, e.file_name, e.user_id, v.sensitivity_rating,
                   v.summary, v.created_at
            FROM verdicts v
            JOIN events e ON v.event_id = e.event_id
            WHERE v.sensitivity_rating >= 4
            ORDER BY v.created_at DESC
            LIMIT 10
        """)

    return {
        "events": dict(event_counts),
        "verdicts": dict(verdict_stats) if verdict_stats else {},
        "by_provider": [dict(r) for r in by_provider],
        "by_rating": [dict(r) for r in by_rating],
        "recent_high_risk": [dict(r) for r in recent_high],
    }
