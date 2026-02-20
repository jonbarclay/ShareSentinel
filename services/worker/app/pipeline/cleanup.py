"""Temp file cleanup for the worker tmpfs mount."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Default tmpfs base path
_DEFAULT_TMPFS = "/tmp/sharesentinel"


class Cleanup:
    """Delete temporary files created during event processing.

    Provides both per-event cleanup (called at the end of every pipeline
    run) and stale-file cleanup (called periodically as a background task).
    """

    @staticmethod
    def cleanup_event_files(event_id: str, tmpfs_path: str = _DEFAULT_TMPFS) -> None:
        """Remove the event subdirectory ``{tmpfs_path}/{event_id}/``.

        Logs a warning if any files remain after the deletion attempt.
        """
        event_dir = Path(tmpfs_path) / event_id
        if not event_dir.exists():
            logger.debug(
                "Event directory does not exist (already cleaned?): %s", event_dir
            )
            return

        try:
            shutil.rmtree(event_dir)
            logger.info("Cleaned up event directory: %s", event_dir)
        except Exception:
            logger.exception("Failed to remove event directory: %s", event_dir)

        # Verify deletion
        if event_dir.exists():
            remaining = list(event_dir.rglob("*"))
            logger.warning(
                "Event directory still exists after cleanup: %s (%d items remaining)",
                event_dir,
                len(remaining),
            )

    @staticmethod
    def cleanup_stale_files(
        tmpfs_path: str = _DEFAULT_TMPFS,
        max_age_minutes: int = 30,
    ) -> int:
        """Scan the tmpfs base directory for stale event directories and remove them.

        An event directory is considered stale if its modification time is
        older than *max_age_minutes* minutes ago.

        Returns the number of directories removed.
        """
        base = Path(tmpfs_path)
        if not base.exists():
            logger.debug("tmpfs path does not exist: %s", base)
            return 0

        cutoff = time.time() - (max_age_minutes * 60)
        removed = 0

        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                logger.warning("Could not stat directory: %s", child)
                continue

            if mtime < cutoff:
                try:
                    shutil.rmtree(child)
                    logger.warning(
                        "Removed stale event directory: %s (age %.0f min)",
                        child.name,
                        (time.time() - mtime) / 60,
                    )
                    removed += 1
                except Exception:
                    logger.exception("Failed to remove stale directory: %s", child)

        if removed:
            logger.info("Stale cleanup complete: removed %d directories", removed)
        return removed
