"""File downloader: stream files from Graph API to the tmpfs mount."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import Config
from ..graph_api.client import AccessDeniedError, FileNotFoundError, GraphClient

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when the download fails for a non-retryable reason."""

    def __init__(self, message: str, reason: str = "download_failed") -> None:
        super().__init__(message)
        self.reason = reason


# event_id must be a hex SHA-256 digest (64 hex chars) or a UUID
_EVENT_ID_RE = re.compile(r"^[a-f0-9]{64}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")

# Child event IDs: {parent_hex}:child:{index} or {uuid}:child:{index}
_CHILD_EVENT_ID_RE = re.compile(
    r"^([a-f0-9]{64}):child:(\d+)$|^([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}):child:(\d+)$"
)


def _extract_dir_event_id(event_id: str) -> tuple[str, int | None]:
    """Extract directory-safe event ID and optional child index.

    Returns ``(parent_hex, None)`` for parent IDs and
    ``(parent_hex, child_index)`` for child IDs.
    Raises ``DownloadError`` if the format is invalid.
    """
    if _EVENT_ID_RE.match(event_id):
        return event_id, None

    m = _CHILD_EVENT_ID_RE.match(event_id)
    if m:
        # Groups 1,2 for hex-sha256 pattern; groups 3,4 for UUID pattern
        parent = m.group(1) or m.group(3)
        child_idx = int(m.group(2) or m.group(4))
        return parent, child_idx

    raise DownloadError(
        f"Invalid event_id format: {event_id!r}",
        reason="invalid_event_id",
    )


class FileDownloader:
    """Download a file from Graph API to the local tmpfs mount.

    Creates an event-specific subdirectory under the configured tmpfs path
    to prevent filename collisions between concurrent jobs.
    """

    async def download(
        self,
        drive_id: str,
        item_id: str,
        event_id: str,
        file_name: str,
        graph_client: GraphClient,
        config: Config,
    ) -> Path:
        """Stream-download a file and return the local path.

        Parameters
        ----------
        drive_id:
            The Graph API drive ID (from metadata pre-screen).
        item_id:
            The Graph API item ID (from metadata pre-screen).
        event_id:
            Unique event identifier (used as subdirectory name).
        file_name:
            Original file name to use on disk.
        graph_client:
            Authenticated ``GraphClient`` instance.
        config:
            Worker ``Config`` for ``tmpfs_path``.

        Returns
        -------
        Path
            Absolute path to the downloaded file on the tmpfs mount.

        Raises
        ------
        DownloadError
            If the file is not found (404) or access is denied (403).
        """
        if not drive_id or not item_id:
            raise DownloadError(
                "Missing drive_id or item_id; cannot download.",
                reason="missing_identifiers",
            )

        # Validate event_id format to prevent path traversal
        dir_id, child_idx = _extract_dir_event_id(event_id)

        # Sanitize file_name — strip path separators and traversal sequences
        safe_name = Path(file_name).name.replace("\x00", "")
        if not safe_name or safe_name in (".", ".."):
            safe_name = f"download_{item_id}"

        # Prefix child filenames to prevent collisions within the shared directory
        if child_idx is not None:
            safe_name = f"child{child_idx}_{safe_name}"

        # Build destination path: /tmp/sharesentinel/{dir_id}/{file_name}
        tmpfs_base = Path(config.tmpfs_path).resolve()
        event_dir = Path(config.tmpfs_path) / dir_id
        dest_path = event_dir / safe_name

        # Verify resolved path stays within tmpfs mount
        if not dest_path.resolve().is_relative_to(tmpfs_base):
            raise DownloadError(
                f"Path traversal detected: {dest_path} escapes {tmpfs_base}",
                reason="path_traversal",
            )

        logger.info(
            "Downloading file event_id=%s drive=%s item=%s -> %s",
            event_id,
            drive_id,
            item_id,
            dest_path,
        )

        try:
            await graph_client.download_file(drive_id, item_id, dest_path)
        except FileNotFoundError:
            logger.warning(
                "File not found (deleted or unshared) event_id=%s item=%s",
                event_id,
                item_id,
            )
            raise DownloadError(
                f"File not found on Graph API for item {item_id}.",
                reason="file_not_found",
            )
        except AccessDeniedError:
            logger.error(
                "Access denied downloading file event_id=%s item=%s",
                event_id,
                item_id,
            )
            raise DownloadError(
                f"Access denied for item {item_id}. Check Azure AD app permissions.",
                reason="access_denied",
            )

        if not dest_path.exists():
            raise DownloadError(
                f"Download appeared to succeed but file not found at {dest_path}.",
                reason="download_failed",
            )

        file_size = dest_path.stat().st_size
        logger.info(
            "Download complete event_id=%s file=%s size=%d bytes",
            event_id,
            file_name,
            file_size,
        )
        return dest_path

    async def download_converted(
        self,
        drive_id: str,
        item_id: str,
        event_id: str,
        file_name: str,
        output_format: str,
        graph_client: GraphClient,
        config: Config,
    ) -> Path:
        """Download a file with server-side format conversion.

        Mirrors :meth:`download` but calls
        :meth:`GraphClient.download_file_converted` and writes the
        output with the converted extension (``.html`` or ``.pdf``).

        Parameters
        ----------
        output_format:
            ``"html"`` or ``"pdf"`` — passed to the Graph API
            ``?format=`` parameter.
        """
        if not drive_id or not item_id:
            raise DownloadError(
                "Missing drive_id or item_id; cannot download.",
                reason="missing_identifiers",
            )

        dir_id, child_idx = _extract_dir_event_id(event_id)

        # Build converted filename: replace extension with target format
        stem = Path(file_name).stem or f"download_{item_id}"
        safe_stem = Path(stem).name.replace("\x00", "")
        if not safe_stem or safe_stem in (".", ".."):
            safe_stem = f"download_{item_id}"
        converted_name = f"{safe_stem}.{output_format}"

        if child_idx is not None:
            converted_name = f"child{child_idx}_{converted_name}"

        tmpfs_base = Path(config.tmpfs_path).resolve()
        event_dir = Path(config.tmpfs_path) / dir_id
        dest_path = event_dir / converted_name

        if not dest_path.resolve().is_relative_to(tmpfs_base):
            raise DownloadError(
                f"Path traversal detected: {dest_path} escapes {tmpfs_base}",
                reason="path_traversal",
            )

        logger.info(
            "Downloading converted file event_id=%s drive=%s item=%s format=%s -> %s",
            event_id, drive_id, item_id, output_format, dest_path,
        )

        try:
            await graph_client.download_file_converted(
                drive_id, item_id, dest_path, output_format,
            )
        except FileNotFoundError:
            logger.warning(
                "File not found (deleted or unshared) event_id=%s item=%s",
                event_id, item_id,
            )
            raise DownloadError(
                f"File not found on Graph API for item {item_id}.",
                reason="file_not_found",
            )
        except AccessDeniedError:
            logger.error(
                "Access denied downloading converted file event_id=%s item=%s",
                event_id, item_id,
            )
            raise DownloadError(
                f"Access denied for item {item_id}. Check Azure AD app permissions.",
                reason="access_denied",
            )

        if not dest_path.exists():
            raise DownloadError(
                f"Converted download appeared to succeed but file not found at {dest_path}.",
                reason="download_failed",
            )

        file_size = dest_path.stat().st_size
        logger.info(
            "Converted download complete event_id=%s file=%s format=%s size=%d bytes",
            event_id, file_name, output_format, file_size,
        )
        return dest_path
