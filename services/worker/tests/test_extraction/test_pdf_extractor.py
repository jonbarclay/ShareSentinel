"""Tests for PDFExtractor with mocked fitz (PyMuPDF)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.extraction.pdf_extractor import PDFExtractor


@pytest.fixture
def extractor() -> PDFExtractor:
    return PDFExtractor()


class TestPDFExtractor:
    """Test suite for PDF text extraction."""

    @patch("app.extraction.pdf_extractor.fitz")
    def test_successful_extraction(self, mock_fitz: MagicMock, extractor: PDFExtractor) -> None:
        """Extract text from a multi-page PDF."""
        mock_page1 = MagicMock()
        mock_page1.get_text.return_value = "Page one content with enough text to pass threshold easily."
        mock_page2 = MagicMock()
        mock_page2.get_text.return_value = "Page two has different content for analysis."

        mock_doc = MagicMock()
        mock_doc.page_count = 2
        mock_doc.metadata = {
            "title": "Test PDF",
            "author": "Test Author",
            "subject": "Testing",
            "creator": "pytest",
            "producer": "test",
        }
        mock_doc.__getitem__ = lambda self, idx: [mock_page1, mock_page2][idx]
        mock_fitz.open.return_value = mock_doc

        result = extractor.extract(Path("/tmp/test.pdf"), 1024)

        assert result.success is True
        assert result.extraction_method == "pdf_text"
        assert "Page one content" in result.text_content
        assert "Page two" in result.text_content
        assert "--- Page 1 ---" in result.text_content
        assert "--- Page 2 ---" in result.text_content
        assert result.metadata["page_count"] == 2
        assert result.metadata["title"] == "Test PDF"
        assert result.content_length > 0
        mock_doc.close.assert_called_once()

    @patch("app.extraction.pdf_extractor.fitz")
    def test_scanned_pdf_insufficient_text(self, mock_fitz: MagicMock, extractor: PDFExtractor) -> None:
        """Scanned PDF with no embedded text returns failure."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = ""

        mock_doc = MagicMock()
        mock_doc.page_count = 1
        mock_doc.metadata = {"title": "", "author": "", "subject": "", "creator": "", "producer": ""}
        mock_doc.__getitem__ = lambda self, idx: mock_page
        mock_fitz.open.return_value = mock_doc

        result = extractor.extract(Path("/tmp/scanned.pdf"), 2048)

        assert result.success is False
        assert "scanned" in result.error.lower()
        assert len(result.warnings) > 0

    @patch("app.extraction.pdf_extractor.fitz")
    def test_truncation_for_large_pdf(self, mock_fitz: MagicMock, extractor: PDFExtractor) -> None:
        """Large PDF text is truncated to MAX_TEXT_SIZE."""
        large_text = "A" * 60_000  # Each page has 60K chars
        mock_page = MagicMock()
        mock_page.get_text.return_value = large_text

        mock_doc = MagicMock()
        mock_doc.page_count = 2
        mock_doc.metadata = {"title": "", "author": "", "subject": "", "creator": "", "producer": ""}
        mock_doc.__getitem__ = lambda self, idx: mock_page
        mock_fitz.open.return_value = mock_doc

        result = extractor.extract(Path("/tmp/large.pdf"), 500_000)

        assert result.success is True
        assert result.was_sampled is True
        assert len(result.text_content) <= extractor.MAX_TEXT_SIZE
        assert result.sampling_description != ""

    @patch("app.extraction.pdf_extractor.fitz")
    def test_exception_handling(self, mock_fitz: MagicMock, extractor: PDFExtractor) -> None:
        """Exception during extraction returns failed result."""
        mock_fitz.open.side_effect = RuntimeError("Corrupted PDF")

        result = extractor.extract(Path("/tmp/corrupt.pdf"), 1024)

        assert result.success is False
        assert result.extraction_method == "pdf_text"
        assert "Corrupted PDF" in result.error

    @patch("app.extraction.pdf_extractor.fitz")
    def test_metadata_populated(self, mock_fitz: MagicMock, extractor: PDFExtractor) -> None:
        """Document metadata is correctly extracted."""
        mock_page = MagicMock()
        mock_page.get_text.return_value = "Sufficient text content for extraction to succeed properly here."

        mock_doc = MagicMock()
        mock_doc.page_count = 1
        mock_doc.metadata = {
            "title": "Confidential Report",
            "author": "Jane Doe",
            "subject": "HR Data",
            "creator": "Word",
            "producer": "macOS",
        }
        mock_doc.__getitem__ = lambda self, idx: mock_page
        mock_fitz.open.return_value = mock_doc

        result = extractor.extract(Path("/tmp/meta.pdf"), 512)

        assert result.metadata["title"] == "Confidential Report"
        assert result.metadata["author"] == "Jane Doe"
        assert result.metadata["subject"] == "HR Data"
