"""File downloader: stream files from Graph API to the tmpfs mount."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config
from ..graph_api.client import AccessDeniedError, FileNotFoundError, GraphClient

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when the download fails for a non-retryable reason."""

    def __init__(self, message: str, reason: str = "download_failed") -> None:
        super().__init__(message)
        self.reason = reason


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

        # Build destination path: /tmp/sharesentinel/{event_id}/{file_name}
        event_dir = Path(config.tmpfs_path) / event_id
        dest_path = event_dir / file_name

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
