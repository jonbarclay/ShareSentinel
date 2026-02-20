from typing import Optional

from .base import BaseExtractor, ExtractionResult
from .pdf_extractor import PDFExtractor
from .docx_extractor import DocxExtractor
from .xlsx_extractor import XlsxExtractor
from .pptx_extractor import PptxExtractor
from .csv_extractor import CsvExtractor
from .text_extractor import TextExtractor
from .archive_extractor import ArchiveExtractor
from .ocr_extractor import OcrExtractor
from .onenote_extractor import OnenoteExtractor
from .transcript_extractor import TranscriptExtractor


def get_extractor(file_extension: str) -> Optional[BaseExtractor]:
    """Return the appropriate extractor for the given file extension."""
    extractors: dict[str, BaseExtractor] = {
        ".pdf": PDFExtractor(),
        ".docx": DocxExtractor(),
        ".doc": DocxExtractor(),
        ".xlsx": XlsxExtractor(),
        ".xls": XlsxExtractor(),
        ".pptx": PptxExtractor(),
        ".ppt": PptxExtractor(),
        ".csv": CsvExtractor(),
        ".tsv": CsvExtractor(),
        ".txt": TextExtractor(),
        ".log": TextExtractor(),
        ".md": TextExtractor(),
        ".json": TextExtractor(),
        ".xml": TextExtractor(),
        ".html": TextExtractor(),
        ".htm": TextExtractor(),
        ".rtf": TextExtractor(),
        ".zip": ArchiveExtractor(),
        ".rar": ArchiveExtractor(),
        ".7z": ArchiveExtractor(),
        ".vtt": TranscriptExtractor(),
        ".srt": TranscriptExtractor(),
        ".one": OnenoteExtractor(),
    }
    return extractors.get(file_extension.lower())


__all__ = [
    "BaseExtractor",
    "ExtractionResult",
    "PDFExtractor",
    "DocxExtractor",
    "XlsxExtractor",
    "PptxExtractor",
    "CsvExtractor",
    "TextExtractor",
    "ArchiveExtractor",
    "OcrExtractor",
    "OnenoteExtractor",
    "TranscriptExtractor",
    "get_extractor",
]
