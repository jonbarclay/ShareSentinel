"""Tests for TextExtractor using temporary files."""

import tempfile
from pathlib import Path

import pytest

from app.extraction.text_extractor import TextExtractor


@pytest.fixture
def extractor() -> TextExtractor:
    return TextExtractor()


class TestTextExtractor:
    """Test suite for plain text file extraction."""

    def test_utf8_text_extraction(self, extractor: TextExtractor) -> None:
        """Extract a basic UTF-8 text file."""
        content = "This is a test document with enough characters to pass the minimum threshold for extraction."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content.encode("utf-8")))
        path.unlink()

        assert result.success is True
        assert result.extraction_method == "text_direct"
        assert result.text_content == content
        assert result.metadata["encoding_detected"] == "utf-8"
        assert result.metadata["file_extension"] == ".txt"
        assert result.was_sampled is False

    def test_latin1_fallback(self, extractor: TextExtractor) -> None:
        """Fall back to latin-1 when UTF-8 decoding fails."""
        # Latin-1 character that is invalid as standalone UTF-8
        raw_bytes = b"Hello \xe9\xe8\xf1 world, this is enough text to pass the threshold for sure."
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(raw_bytes)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(raw_bytes))
        path.unlink()

        assert result.success is True
        assert result.metadata["encoding_detected"] == "latin-1"

    def test_large_file_sampling(self, extractor: TextExtractor) -> None:
        """Files larger than 100KB report was_sampled=True."""
        # Create content larger than MAX_READ_BYTES
        content = "x" * 150_000
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, 150_000)
        path.unlink()

        assert result.success is True
        assert result.was_sampled is True
        assert "100KB" in result.sampling_description
        assert len(result.text_content) <= extractor.MAX_READ_BYTES

    def test_empty_file(self, extractor: TextExtractor) -> None:
        """Empty text file returns failure."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("")
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, 0)
        path.unlink()

        assert result.success is False
        assert "empty" in result.error.lower()

    def test_json_file(self, extractor: TextExtractor) -> None:
        """JSON files are read as raw text."""
        content = '{"employees": [{"name": "Alice", "ssn": "123-45-6789"}]}'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert result.metadata["file_extension"] == ".json"
        assert "123-45-6789" in result.text_content

    def test_nonexistent_file(self, extractor: TextExtractor) -> None:
        """Non-existent file returns error result."""
        result = extractor.extract(Path("/tmp/no_such_file_99999.txt"), 100)

        assert result.success is False
        assert result.error is not None

    def test_markdown_file(self, extractor: TextExtractor) -> None:
        """Markdown files are read as raw text."""
        content = "# Heading\n\nThis is a markdown document with sufficient content for extraction threshold."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert result.metadata["file_extension"] == ".md"
        assert "# Heading" in result.text_content

    def test_html_file(self, extractor: TextExtractor) -> None:
        """HTML files are read as raw text (not parsed)."""
        content = "<html><body><p>Sensitive data: SSN 999-88-7777</p></body></html>"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert "999-88-7777" in result.text_content
