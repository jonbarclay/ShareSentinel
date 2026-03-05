from pathlib import Path

import pytest

from app.main import _safe_upload_path, _sanitize_upload_filename


def test_sanitize_upload_filename_strips_path_traversal():
    assert _sanitize_upload_filename("../../etc/passwd") == "passwd"


def test_safe_upload_path_rejects_escape(tmp_path: Path):
    with pytest.raises(ValueError):
        _safe_upload_path(tmp_path, "../evil.txt")


def test_safe_upload_path_allows_normal_file(tmp_path: Path):
    result = _safe_upload_path(tmp_path, "clip.mp4")
    assert result == (tmp_path / "clip.mp4").resolve()

