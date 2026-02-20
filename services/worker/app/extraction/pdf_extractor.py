"""PDF text extraction using PyMuPDF (fitz)."""

from pathlib import Path

import fitz  # PyMuPDF

from .base import BaseExtractor, ExtractionResult


class PDFExtractor(BaseExtractor):
    """Extract text from PDF files using PyMuPDF."""

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            doc = fitz.open(str(file_path))
            metadata = {
                "page_count": doc.page_count,
                "title": doc.metadata.get("title", ""),
                "author": doc.metadata.get("author", ""),
                "subject": doc.metadata.get("subject", ""),
                "creator": doc.metadata.get("creator", ""),
                "producer": doc.metadata.get("producer", ""),
            }

            all_text: list[str] = []
            for page_num in range(doc.page_count):
                page = doc[page_num]
                page_text = page.get_text("text")
                all_text.append(f"--- Page {page_num + 1} ---\n{page_text}")

            full_text = "\n".join(all_text)
            doc.close()

            # Check if extraction produced meaningful content
            stripped = full_text.strip()
            if len(stripped) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="pdf_text",
                    metadata=metadata,
                    error="PDF appears to be scanned/image-based (insufficient text extracted)",
                    warnings=[
                        "Text extraction produced < 50 characters; likely a scanned document"
                    ],
                )

            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"First ~100KB of text from a {metadata['page_count']}-page PDF",
            )

            return ExtractionResult(
                success=True,
                extraction_method="pdf_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=description,
                content_length=len(text),
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="pdf_text",
                error=str(e),
            )
