"""File hashing and deduplication check."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from ..database.repositories import FileHashRepository

logger = logging.getLogger(__name__)

# Read in 64 KB chunks when hashing
_HASH_CHUNK_SIZE = 65_536


class FileHasher:
    """Compute SHA-256 hashes and check for previously-analysed identical files.

    Implements pipeline Step 7: after downloading a file, compute its hash
    and look for a recent verdict in the ``file_hashes`` table.  If a match
    is found within ``max_age_days``, the previous verdict can be reused.
    """

    @staticmethod
    def compute_hash(file_path: Path) -> str:
        """Return the hex-encoded SHA-256 hash of the file at *file_path*.

        Reads the file in fixed-size chunks so that large files do not
        need to fit entirely in memory.
        """
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                sha.update(chunk)
        digest = sha.hexdigest()
        logger.debug("Computed SHA-256 for %s: %s", file_path.name, digest)
        return digest

    @staticmethod
    async def check_reuse(
        file_hash: str,
        file_hash_repo: FileHashRepository,
        max_age_days: int = 30,
    ) -> Optional[Dict[str, Any]]:
        """Check whether *file_hash* was already analysed within *max_age_days*.

        Returns
        -------
        dict or None
            If a match is found, returns the ``file_hashes`` row as a dict
            containing at least ``first_event_id``, ``sensitivity_rating``,
            ``times_seen``, and ``last_seen_at``.  Returns ``None`` if no
            recent match exists.
        """
        existing = await file_hash_repo.check_hash(file_hash, max_age_days)
        if existing:
            logger.info(
                "Hash reuse match hash=%s…%s previous_event=%s rating=%s",
                file_hash[:8],
                file_hash[-4:],
                existing.get("first_event_id"),
                existing.get("sensitivity_rating"),
            )
            # Bump the seen counter / timestamp
            await file_hash_repo.update_last_seen(file_hash)
            return existing

        logger.debug("No hash reuse match for %s…%s", file_hash[:8], file_hash[-4:])
        return None

    @staticmethod
    async def store_hash(
        file_hash: str,
        event_id: str,
        sensitivity_rating: int,
        file_hash_repo: FileHashRepository,
    ) -> None:
        """Persist the hash after a new verdict is recorded.

        Uses ``INSERT … ON CONFLICT`` so that a race between two workers
        processing the same content does not produce an error.
        """
        await file_hash_repo.store_hash(file_hash, event_id, sensitivity_rating)
        logger.info(
            "Stored hash=%s…%s event_id=%s rating=%s",
            file_hash[:8],
            file_hash[-4:],
            event_id,
            sensitivity_rating,
        )
