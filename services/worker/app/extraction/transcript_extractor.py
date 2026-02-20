"""Transcript extractor: wraps pre-fetched transcript text into an ExtractionResult.

Used for:
- Graph API VTT transcripts (Teams recordings)
- Whisper transcription output
- Directly shared .vtt / .srt subtitle files
"""

from __future__ import annotations

import logging
import re
from html import unescape
from pathlib import Path
from typing import Optional

from .base import BaseExtractor, ExtractionResult

logger = logging.getLogger(__name__)


class TranscriptExtractor(BaseExtractor):
    """Extract text from VTT or SRT subtitle/transcript files.

    For pre-fetched transcript text (Graph API, Whisper), use
    ``from_text()`` instead of ``extract()``.
    """

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        """Extract text from a .vtt or .srt file on disk."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return ExtractionResult(
                success=False,
                extraction_method="transcript_file",
                error=str(exc),
            )

        ext = file_path.suffix.lower()
        if ext == ".vtt":
            text = self._parse_vtt(content)
        elif ext == ".srt":
            text = self._parse_srt(content)
        else:
            text = content

        if not text or len(text) < self.MIN_MEANINGFUL_TEXT:
            return ExtractionResult(
                success=False,
                extraction_method="transcript_file",
                error="Transcript contained no meaningful text",
            )

        text, was_sampled, sampling_desc = self._truncate_if_needed(
            text, f"Transcript truncated from {len(text):,} to {self.MAX_TEXT_SIZE:,} characters."
        )

        return ExtractionResult(
            success=True,
            extraction_method="transcript_file",
            text_content=text,
            content_length=len(text),
            was_sampled=was_sampled,
            sampling_description=sampling_desc,
            metadata={"source_format": ext.lstrip(".")},
        )

    @classmethod
    def from_text(
        cls,
        transcript_text: str,
        source: str = "graph_api",
        duration_seconds: Optional[int] = None,
    ) -> ExtractionResult:
        """Wrap pre-fetched transcript text into an ExtractionResult.

        Parameters
        ----------
        transcript_text:
            Plain text transcript (already parsed from VTT/Whisper output).
        source:
            Origin of the transcript (``"graph_api"`` or ``"whisper"``).
        duration_seconds:
            Media duration in seconds (from Whisper), if known.
        """
        if not transcript_text or len(transcript_text) < cls.MIN_MEANINGFUL_TEXT:
            return ExtractionResult(
                success=False,
                extraction_method=f"transcript_{source}",
                error="Transcript contained no meaningful text",
            )

        was_sampled = False
        sampling_desc = ""
        if len(transcript_text) > cls.MAX_TEXT_SIZE:
            transcript_text = transcript_text[: cls.MAX_TEXT_SIZE]
            was_sampled = True
            sampling_desc = (
                f"Transcript truncated to {cls.MAX_TEXT_SIZE:,} characters "
                f"(source: {source})."
            )

        metadata = {"transcript_source": source}
        if duration_seconds is not None:
            metadata["media_duration_seconds"] = duration_seconds

        return ExtractionResult(
            success=True,
            extraction_method=f"transcript_{source}",
            text_content=transcript_text,
            content_length=len(transcript_text),
            was_sampled=was_sampled,
            sampling_description=sampling_desc,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # VTT / SRT parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_vtt(content: str) -> str:
        """Parse WebVTT content to plain text."""
        lines = content.splitlines()
        text_lines: list[str] = []
        prev_line = ""
        timestamp_re = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
        tag_re = re.compile(r"</?[^>]+>")
        in_header = True

        for line in lines:
            stripped = line.strip()
            if in_header:
                if not stripped or stripped.startswith("WEBVTT") or stripped.startswith("NOTE"):
                    continue
                in_header = False
            if not stripped or stripped.isdigit() or timestamp_re.match(stripped):
                continue
            clean = tag_re.sub("", stripped)
            clean = unescape(clean).strip()
            if clean and clean != prev_line:
                text_lines.append(clean)
                prev_line = clean

        return "\n".join(text_lines)

    @staticmethod
    def _parse_srt(content: str) -> str:
        """Parse SRT subtitle content to plain text."""
        lines = content.splitlines()
        text_lines: list[str] = []
        prev_line = ""
        timestamp_re = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s*-->")
        tag_re = re.compile(r"</?[^>]+>")

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.isdigit() or timestamp_re.match(stripped):
                continue
            clean = tag_re.sub("", stripped)
            clean = unescape(clean).strip()
            if clean and clean != prev_line:
                text_lines.append(clean)
                prev_line = clean

        return "\n".join(text_lines)
