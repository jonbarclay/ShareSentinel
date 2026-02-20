"""PostgreSQL connection management using asyncpg."""

import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)


async def create_pool() -> asyncpg.Pool:
    """Create and return a connection pool."""
    dsn = os.environ.get("DATABASE_URL", "")
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    logger.info("Database connection pool created")
    return pool


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Run pending database migrations."""
    async with pool.acquire() as conn:
        # Ensure schema_migrations table exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INT PRIMARY KEY,
                filename VARCHAR(255) NOT NULL,
                applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
        """)

        # Get already-applied versions
        applied = {
            row["version"]
            for row in await conn.fetch("SELECT version FROM schema_migrations")
        }

        # Find and run pending migrations
        migrations_dir = Path(__file__).parent / "migrations"
        if not migrations_dir.exists():
            return

        migration_files = sorted(migrations_dir.glob("*.sql"))
        for mf in migration_files:
            version = int(mf.stem.split("_")[0])
            if version in applied:
                continue

            logger.info(f"Running migration {mf.name}")
            sql = mf.read_text()
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)",
                version,
                mf.name,
            )
            logger.info(f"Migration {mf.name} applied")
