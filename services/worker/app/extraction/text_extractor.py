"""Plain text file extraction for .txt, .log, .md, .json, .xml, .html, .rtf."""

from pathlib import Path

from .base import BaseExtractor, ExtractionResult


class TextExtractor(BaseExtractor):
    """Extract text from plain text files. Tries UTF-8, falls back to latin-1."""

    MAX_READ_BYTES = 100_000  # 100KB

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            # Try UTF-8 first, fall back to latin-1
            encoding = "utf-8"
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read(self.MAX_READ_BYTES)
            except UnicodeDecodeError:
                encoding = "latin-1"
                with open(file_path, "r", encoding="latin-1") as f:
                    content = f.read(self.MAX_READ_BYTES)

            was_sampled = file_size > self.MAX_READ_BYTES

            metadata = {
                "file_size_bytes": file_size,
                "encoding_detected": encoding,
                "file_extension": file_path.suffix.lower(),
            }

            if len(content.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="text_direct",
                    metadata=metadata,
                    error="File appears to be empty or contains very little text",
                )

            return ExtractionResult(
                success=True,
                extraction_method="text_direct",
                text_content=content,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=(
                    f"First 100KB of a {file_size / 1024:.0f}KB file"
                    if was_sampled
                    else ""
                ),
                content_length=len(content),
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="text_direct",
                error=str(e),
            )
