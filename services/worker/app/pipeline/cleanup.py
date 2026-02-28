"""Temp file cleanup for the worker tmpfs mount."""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Default tmpfs base path
_DEFAULT_TMPFS = "/tmp/sharesentinel"

# event_id must be a hex SHA-256 digest (64 hex chars)
_EVENT_ID_RE = re.compile(r"^[a-f0-9]{64}$")

# Child event IDs: {parent_hex}:child:{index}
_CHILD_EVENT_ID_RE = re.compile(r"^([a-f0-9]{64}):child:(\d+)$")


def _extract_parent_hex(event_id: str) -> str | None:
    """Return the 64-hex directory name for *event_id*, or ``None`` if invalid.

    Accepts both parent IDs (``<hex64>``) and child IDs
    (``<hex64>:child:<n>``).
    """
    if _EVENT_ID_RE.match(event_id):
        return event_id
    m = _CHILD_EVENT_ID_RE.match(event_id)
    if m:
        return m.group(1)
    return None


class Cleanup:
    """Delete temporary files created during event processing.

    Provides both per-event cleanup (called at the end of every pipeline
    run) and stale-file cleanup (called periodically as a background task).
    """

    @staticmethod
    def cleanup_event_files(event_id: str, tmpfs_path: str = _DEFAULT_TMPFS) -> None:
        """Remove the event subdirectory ``{tmpfs_path}/{event_id}/``.

        Accepts both parent (``<hex64>``) and child (``<hex64>:child:<n>``)
        event IDs — in either case the *parent hex* is used as the directory
        name.  Logs a warning if any files remain after the deletion attempt.
        """
        # Validate event_id format to prevent path traversal
        dir_name = _extract_parent_hex(event_id)
        if dir_name is None:
            logger.error("Invalid event_id format for cleanup: %r", event_id)
            return

        event_dir = Path(tmpfs_path) / dir_name

        # Verify resolved path stays within tmpfs mount
        tmpfs_base = Path(tmpfs_path).resolve()
        if not event_dir.resolve().is_relative_to(tmpfs_base):
            logger.error("Path traversal detected in cleanup: %s", event_dir)
            return

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
    def cleanup_child_file(
        event_id: str,
        file_path: Path,
        tmpfs_path: str = _DEFAULT_TMPFS,
    ) -> None:
        """Delete a single child file without removing the parent directory.

        Used during folder enumeration so that sibling files are not wiped
        while other children are still being processed.
        """
        dir_name = _extract_parent_hex(event_id)
        if dir_name is None:
            logger.error("Invalid event_id format for child cleanup: %r", event_id)
            return

        tmpfs_base = Path(tmpfs_path).resolve()
        resolved = file_path.resolve()
        if not resolved.is_relative_to(tmpfs_base):
            logger.error("Path traversal detected in child cleanup: %s", file_path)
            return

        if not resolved.exists():
            logger.debug("Child file already removed: %s", file_path)
            return

        try:
            resolved.unlink()
            logger.info("Cleaned up child file: %s", file_path)
        except Exception:
            logger.exception("Failed to remove child file: %s", file_path)

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
