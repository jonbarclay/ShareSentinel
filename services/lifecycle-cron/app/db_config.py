"""Load configuration overrides from the database."""

from typing import Dict


async def load_db_overrides(pool) -> Dict[str, str]:
    """Read non-empty config values from the configuration table."""
    rows = await pool.fetch("SELECT key, value FROM configuration WHERE value != ''")
    return {row["key"]: row["value"] for row in rows}
