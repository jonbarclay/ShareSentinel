"""Tests for CsvExtractor using temporary files."""

import tempfile
from pathlib import Path

import pytest

from app.extraction.csv_extractor import CsvExtractor


@pytest.fixture
def extractor() -> CsvExtractor:
    return CsvExtractor()


class TestCsvExtractor:
    """Test suite for CSV/TSV text extraction."""

    def test_basic_csv_extraction(self, extractor: CsvExtractor) -> None:
        """Extract a simple CSV with header and data rows."""
        content = "name,email,phone\nAlice,alice@example.com,555-0001\nBob,bob@example.com,555-0002\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert result.extraction_method == "csv_text"
        assert "Alice" in result.text_content
        assert "Bob" in result.text_content
        assert result.metadata["total_rows"] == 3  # header + 2 data rows
        assert result.metadata["delimiter"] == ","

    def test_tsv_extraction(self, extractor: CsvExtractor) -> None:
        """Extract a TSV file with tab delimiters."""
        content = "name\temail\nAlice\talice@example.com\nBob\tbob@example.com\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tsv", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert "Alice" in result.text_content

    def test_large_csv_sampling(self, extractor: CsvExtractor) -> None:
        """CSV with more than 500 rows is sampled and flagged."""
        lines = ["col1,col2,col3\n"]
        for i in range(700):
            lines.append(f"val{i}_a,val{i}_b,val{i}_c\n")
        content = "".join(lines)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert result.was_sampled is True
        assert result.metadata["total_rows"] == 701  # header + 700 data
        assert result.metadata["rows_extracted"] == 501  # header + 500

    def test_empty_csv(self, extractor: CsvExtractor) -> None:
        """Empty CSV file returns failure."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("")
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, 0)
        path.unlink()

        assert result.success is False
        assert "empty" in result.error.lower()

    def test_nonexistent_file(self, extractor: CsvExtractor) -> None:
        """Non-existent file returns error result."""
        result = extractor.extract(Path("/tmp/does_not_exist_12345.csv"), 100)

        assert result.success is False
        assert result.error is not None

    def test_single_column_csv(self, extractor: CsvExtractor) -> None:
        """CSV with a single column still extracts successfully."""
        content = "id\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n" * 5
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)

        result = extractor.extract(path, len(content))
        path.unlink()

        assert result.success is True
        assert result.extraction_method == "csv_text"
