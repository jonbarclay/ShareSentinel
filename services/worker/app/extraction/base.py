"""Base extractor interface and shared data structures for text extraction."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ExtractionResult:
    """Result of a text extraction attempt."""

    success: bool
    extraction_method: str  # "pdf_text", "docx_text", "xlsx_text", "ocr", etc.
    text_content: Optional[str] = None
    metadata: Dict = field(default_factory=dict)
    was_sampled: bool = False
    sampling_description: str = ""
    content_length: int = 0
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


class BaseExtractor(ABC):
    """Base class for all text extractors."""

    MAX_TEXT_SIZE = 100_000  # 100KB character limit (~25K tokens)
    MIN_MEANINGFUL_TEXT = 50  # Minimum characters to consider extraction successful

    @abstractmethod
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        """Extract text content from the file."""
        pass

    def _truncate_if_needed(self, text: str, description: str) -> tuple[str, bool, str]:
        """Truncate text to MAX_TEXT_SIZE if needed."""
        if len(text) <= self.MAX_TEXT_SIZE:
            return text, False, ""
        truncated = text[: self.MAX_TEXT_SIZE]
        return truncated, True, description
