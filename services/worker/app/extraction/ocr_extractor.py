"""OCR extraction for scanned PDFs using pytesseract and PyMuPDF page rendering."""

import io
from pathlib import Path

import fitz  # PyMuPDF for rendering
import pytesseract
from PIL import Image

from .base import BaseExtractor, ExtractionResult


class OcrExtractor(BaseExtractor):
    """Render PDF pages as images and run Tesseract OCR on each."""

    MAX_PAGES = 5
    RENDER_DPI = 150  # 150 DPI is sufficient for OCR text recognition

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            doc = fitz.open(str(file_path))
            page_count = doc.page_count
            pages_to_process = min(page_count, self.MAX_PAGES)

            metadata = {
                "total_pages": page_count,
                "pages_ocr_processed": pages_to_process,
                "ocr_dpi": self.RENDER_DPI,
            }

            parts: list[str] = []
            for page_num in range(pages_to_process):
                page = doc[page_num]
                # Render page to image at specified DPI
                mat = fitz.Matrix(self.RENDER_DPI / 72, self.RENDER_DPI / 72)
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_bytes))

                # Run OCR
                ocr_text = pytesseract.image_to_string(img)

                if ocr_text.strip():
                    parts.append(
                        f"--- Page {page_num + 1} (OCR) ---\n{ocr_text.strip()}"
                    )

            doc.close()

            full_text = "\n\n".join(parts)

            if len(full_text.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="ocr",
                    metadata=metadata,
                    error=(
                        "OCR produced insufficient text (document may be handwritten, "
                        "low quality, or non-text content)"
                    ),
                    warnings=[
                        "OCR extracted < 50 characters; multimodal analysis recommended"
                    ],
                )

            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"OCR text from first {pages_to_process} of {page_count} pages",
            )

            return ExtractionResult(
                success=True,
                extraction_method="ocr",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled or pages_to_process < page_count,
                sampling_description=description
                if was_sampled
                else (
                    f"OCR of first {pages_to_process} of {page_count} pages"
                    if pages_to_process < page_count
                    else ""
                ),
                content_length=len(text),
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="ocr",
                error=str(e),
            )
