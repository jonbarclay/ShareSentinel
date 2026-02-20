"""Tests for event_id validation and path traversal prevention."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.pipeline.downloader import (
    FileDownloader, DownloadError, _EVENT_ID_RE,
    _CHILD_EVENT_ID_RE, _extract_dir_event_id,
)
from app.pipeline.cleanup import (
    Cleanup,
    _EVENT_ID_RE as CLEANUP_EVENT_ID_RE,
    _extract_parent_hex,
)


# ---------------------------------------------------------------------------
# event_id regex tests
# ---------------------------------------------------------------------------

class TestEventIdRegex:
    """Verify the event_id regex only accepts valid SHA-256 hex digests."""

    def test_valid_sha256(self):
        valid = "a" * 64
        assert _EVENT_ID_RE.match(valid)

    def test_valid_mixed_hex(self):
        valid = "0123456789abcdef" * 4
        assert _EVENT_ID_RE.match(valid)

    def test_rejects_short_hash(self):
        assert not _EVENT_ID_RE.match("a" * 63)

    def test_rejects_long_hash(self):
        assert not _EVENT_ID_RE.match("a" * 65)

    def test_rejects_uppercase(self):
        assert not _EVENT_ID_RE.match("A" * 64)

    def test_rejects_path_traversal(self):
        assert not _EVENT_ID_RE.match("../../etc/passwd")

    def test_rejects_slashes(self):
        assert not _EVENT_ID_RE.match("a" * 32 + "/../../" + "a" * 25)

    def test_rejects_null_bytes(self):
        assert not _EVENT_ID_RE.match("a" * 32 + "\x00" + "a" * 31)

    def test_rejects_empty(self):
        assert not _EVENT_ID_RE.match("")

    def test_rejects_special_chars(self):
        assert not _EVENT_ID_RE.match("a" * 62 + "..")

    def test_cleanup_regex_matches_downloader(self):
        """Both modules use the same regex pattern."""
        valid = "a" * 64
        assert CLEANUP_EVENT_ID_RE.match(valid)
        assert not CLEANUP_EVENT_ID_RE.match("../../etc/passwd")


# ---------------------------------------------------------------------------
# Child event ID tests
# ---------------------------------------------------------------------------

class TestChildEventId:
    """Verify child event ID regex and extraction helpers."""

    def test_child_regex_accepts_valid(self):
        parent = "a" * 64
        assert _CHILD_EVENT_ID_RE.match(f"{parent}:child:0")
        assert _CHILD_EVENT_ID_RE.match(f"{parent}:child:42")

    def test_child_regex_rejects_invalid_parent(self):
        assert not _CHILD_EVENT_ID_RE.match("short:child:0")
        assert not _CHILD_EVENT_ID_RE.match("../../etc/passwd:child:0")

    def test_child_regex_rejects_negative_index(self):
        parent = "a" * 64
        assert not _CHILD_EVENT_ID_RE.match(f"{parent}:child:-1")

    def test_child_regex_rejects_non_numeric_index(self):
        parent = "a" * 64
        assert not _CHILD_EVENT_ID_RE.match(f"{parent}:child:abc")

    def test_extract_dir_parent(self):
        parent = "b" * 64
        dir_id, idx = _extract_dir_event_id(parent)
        assert dir_id == parent
        assert idx is None

    def test_extract_dir_child(self):
        parent = "c" * 64
        dir_id, idx = _extract_dir_event_id(f"{parent}:child:7")
        assert dir_id == parent
        assert idx == 7

    def test_extract_dir_invalid_raises(self):
        with pytest.raises(DownloadError, match="Invalid event_id"):
            _extract_dir_event_id("../../etc/passwd")

    def test_extract_dir_rejects_traversal_in_child(self):
        with pytest.raises(DownloadError, match="Invalid event_id"):
            _extract_dir_event_id("../../etc/passwd:child:0")

    def test_cleanup_extract_parent_hex(self):
        parent = "d" * 64
        assert _extract_parent_hex(parent) == parent
        assert _extract_parent_hex(f"{parent}:child:3") == parent
        assert _extract_parent_hex("../../etc/passwd") is None
        assert _extract_parent_hex("invalid") is None


# ---------------------------------------------------------------------------
# FileDownloader path safety tests
# ---------------------------------------------------------------------------

class TestDownloaderPathSafety:
    """Verify FileDownloader rejects malicious event_ids."""

    @pytest.mark.asyncio
    async def test_rejects_traversal_event_id(self):
        downloader = FileDownloader()
        config = MagicMock()
        config.tmpfs_path = "/tmp/sharesentinel"
        graph_client = AsyncMock()

        with pytest.raises(DownloadError, match="Invalid event_id"):
            await downloader.download(
                drive_id="d1",
                item_id="i1",
                event_id="../../etc/passwd",
                file_name="test.txt",
                graph_client=graph_client,
                config=config,
            )

    @pytest.mark.asyncio
    async def test_rejects_non_hex_event_id(self):
        downloader = FileDownloader()
        config = MagicMock()
        config.tmpfs_path = "/tmp/sharesentinel"
        graph_client = AsyncMock()

        with pytest.raises(DownloadError, match="Invalid event_id"):
            await downloader.download(
                drive_id="d1",
                item_id="i1",
                event_id="not-a-valid-hex-hash",
                file_name="test.txt",
                graph_client=graph_client,
                config=config,
            )

    @pytest.mark.asyncio
    async def test_accepts_child_event_id(self, tmp_path):
        """Child event IDs should be accepted and use parent hex as directory."""
        downloader = FileDownloader()
        config = MagicMock()
        config.tmpfs_path = str(tmp_path)
        parent_hex = "a" * 64
        child_event_id = f"{parent_hex}:child:3"

        graph_client = AsyncMock()
        # Simulate a successful download by creating the file
        async def fake_download(drive_id, item_id, dest_path):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text("test content")
        graph_client.download_file = fake_download

        result = await downloader.download(
            drive_id="d1", item_id="i1",
            event_id=child_event_id,
            file_name="report.pdf",
            graph_client=graph_client,
            config=config,
        )
        # Directory should use parent hex, filename should be prefixed
        assert result.parent.name == parent_hex
        assert result.name == "child3_report.pdf"
        assert result.exists()


# ---------------------------------------------------------------------------
# Cleanup path safety tests
# ---------------------------------------------------------------------------

class TestCleanupPathSafety:
    """Verify Cleanup rejects malicious event_ids."""

    def test_rejects_traversal_event_id(self, caplog):
        Cleanup.cleanup_event_files("../../etc", "/tmp/sharesentinel")
        assert "Invalid event_id" in caplog.text

    def test_rejects_slash_in_event_id(self, caplog):
        Cleanup.cleanup_event_files("foo/bar", "/tmp/sharesentinel")
        assert "Invalid event_id" in caplog.text

    def test_accepts_valid_event_id(self, tmp_path):
        """A valid hex event_id that doesn't exist should log 'does not exist'."""
        valid_id = "a" * 64
        Cleanup.cleanup_event_files(valid_id, str(tmp_path))
        # No error — just no-op since directory doesn't exist

    def test_cleanup_accepts_child_event_id(self, tmp_path):
        """cleanup_event_files should accept child IDs and use parent hex."""
        parent = "b" * 64
        event_dir = tmp_path / parent
        event_dir.mkdir()
        (event_dir / "child0_file.txt").write_text("data")
        Cleanup.cleanup_event_files(f"{parent}:child:0", str(tmp_path))
        assert not event_dir.exists()

    def test_cleanup_child_file_deletes_only_target(self, tmp_path):
        """cleanup_child_file removes one file without touching siblings."""
        parent = "c" * 64
        event_dir = tmp_path / parent
        event_dir.mkdir()
        target = event_dir / "child0_report.pdf"
        sibling = event_dir / "child1_budget.xlsx"
        target.write_text("target")
        sibling.write_text("sibling")

        Cleanup.cleanup_child_file(f"{parent}:child:0", target, str(tmp_path))
        assert not target.exists()
        assert sibling.exists()
        assert event_dir.exists()
