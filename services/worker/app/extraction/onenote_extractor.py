"""OneNote (.one) section file text extraction.

Parses the MS-ONESTORE binary format to extract text content (including
historical/deleted revisions) and embedded files.  Uses pyOneNote for
structured parsing with a raw binary sweep fallback.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Optional

from .base import BaseExtractor, ExtractionResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MS-ONESTORE header — first 16 bytes of any valid .one section file
ONESTORE_HEADER = (
    b"\xE4\x52\x5C\x7B\x8C\xD8\xA7\x4D"
    b"\xAE\xB1\x53\x78\xD0\x29\x96\xD3"
)

# FileDataStoreObject header/footer GUIDs for embedded file carving
FDSO_HEADER = (
    b"\xE7\x16\xE3\xBD\x65\x26\x11\x45"
    b"\xA4\xC4\x8D\x4D\x0B\x7A\x9E\xAC"
)
FDSO_FOOTER = (
    b"\x22\xA7\xFB\x71\x79\x0F\x0B\x4A"
    b"\xBB\x13\x89\x92\x56\x42\x6B\x24"
)

# Property IDs for text content in OneNote property sets
PROP_RICH_EDIT_TEXT_UNICODE = 0x1C001C22
PROP_TEXT_EXTENDED_ASCII = 0x1C003498
PROP_CACHED_TITLE_STRING = 0x1C001CF3

MAX_TOTAL_EMBEDDED_BYTES = 50_000_000  # 50 MB cap on cumulative carved data
MAX_RAW_SWEEP_SIZE = 50_000_000  # Only sweep first 50 MB for raw fallback
MIN_UTF16_STRING_CHARS = 20  # Minimum chars for raw sweep string inclusion

# MIME type -> file extension mapping for embedded files
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/rtf": ".rtf",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
}


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

def validate_onestore_header(file_path: str) -> None:
    """Validate that the file starts with the MS-ONESTORE header.

    Raises ``ValueError`` if the file is too small or has the wrong header.
    """
    try:
        with open(file_path, "rb") as f:
            header = f.read(16)
    except OSError as exc:
        raise ValueError(f"Cannot read file: {exc}") from exc

    if len(header) < 16:
        raise ValueError(
            f"File too small ({len(header)} bytes) to be a valid .one file"
        )
    if header != ONESTORE_HEADER:
        raise ValueError(
            "Invalid OneNote file: MS-ONESTORE header not found"
        )


# ---------------------------------------------------------------------------
# pyOneNote-based extraction
# ---------------------------------------------------------------------------

def _extract_via_pyonenote(
    file_path: str,
) -> tuple[list[str], list[dict]]:
    """Parse .one file via pyOneNote and return (text_chunks, embedded_files).

    Each embedded file dict has ``"content"`` (bytes) and ``"extension"`` (str).
    Raises ``ImportError`` if pyOneNote is not installed, or propagates any
    parsing exception.
    """
    import io
    from pyOneNote.OneDocument import OneDocment  # noqa: N813

    with open(file_path, "rb") as f:
        data = f.read()

    doc = OneDocment(io.BytesIO(data))

    text_chunks: list[str] = []
    embedded_files: list[dict] = []

    # Walk all property sets in the document to extract text.
    # pyOneNote exposes the parsed object tree; we walk it exhaustively
    # to capture historical/deleted revisions, not just active content.
    _walk_property_sets(doc, text_chunks, embedded_files)

    return text_chunks, embedded_files


def _walk_property_sets(
    doc: object,
    text_chunks: list[str],
    embedded_files: list[dict],
) -> None:
    """Recursively walk pyOneNote document structures for text and files."""
    # pyOneNote stores parsed data in different attributes depending on version.
    # We try multiple known structures.

    # Try walking the document's property sets
    if hasattr(doc, "root_file_node_list"):
        _walk_node_list(doc.root_file_node_list, text_chunks, embedded_files)

    # Also try direct property access patterns used by various pyOneNote versions
    if hasattr(doc, "property_sets"):
        for ps in doc.property_sets:
            _extract_from_property_set(ps, text_chunks)

    # Walk all file data store objects for embedded files
    if hasattr(doc, "file_data_store"):
        for fdso in doc.file_data_store:
            if hasattr(fdso, "data") and fdso.data:
                embedded_files.append({"content": fdso.data, "extension": ""})


def _walk_node_list(
    node_list: object,
    text_chunks: list[str],
    embedded_files: list[dict],
) -> None:
    """Walk a FileNodeList and its children recursively."""
    if not hasattr(node_list, "children"):
        return
    for child in node_list.children:
        # Extract text from property sets
        if hasattr(child, "property_set"):
            _extract_from_property_set(child.property_set, text_chunks)
        # Recurse into sub-node lists
        if hasattr(child, "children"):
            _walk_node_list(child, text_chunks, embedded_files)
        # Check for file data
        if hasattr(child, "data") and isinstance(getattr(child, "data", None), bytes):
            data = child.data
            if len(data) > 16 and data[:16] != ONESTORE_HEADER:
                embedded_files.append({"content": data, "extension": ""})


def _extract_from_property_set(
    ps: object, text_chunks: list[str]
) -> None:
    """Extract text properties from a single property set object."""
    if ps is None:
        return

    # pyOneNote property sets expose properties as a dict or list
    properties = None
    if hasattr(ps, "properties"):
        properties = ps.properties
    elif hasattr(ps, "rgPrids"):
        properties = ps.rgPrids

    if properties is None:
        return

    # Handle dict-style properties
    if isinstance(properties, dict):
        for prop_id, value in properties.items():
            _try_extract_text_property(prop_id, value, text_chunks)
    # Handle list-style properties
    elif isinstance(properties, (list, tuple)):
        for prop in properties:
            if hasattr(prop, "property_id") and hasattr(prop, "value"):
                _try_extract_text_property(prop.property_id, prop.value, text_chunks)
            elif hasattr(prop, "data"):
                # Some versions store raw text data directly
                _try_decode_text(prop.data, text_chunks)


def _try_extract_text_property(
    prop_id: object, value: object, text_chunks: list[str]
) -> None:
    """Attempt to extract text from a property based on its ID."""
    try:
        pid = int(prop_id) if not isinstance(prop_id, int) else prop_id
    except (ValueError, TypeError):
        return

    if pid in (PROP_RICH_EDIT_TEXT_UNICODE, PROP_CACHED_TITLE_STRING):
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-16-le").strip("\x00").strip()
                if text:
                    text_chunks.append(text)
            except UnicodeDecodeError:
                pass
        elif isinstance(value, str) and value.strip():
            text_chunks.append(value.strip())
    elif pid == PROP_TEXT_EXTENDED_ASCII:
        if isinstance(value, bytes):
            try:
                text = value.decode("ascii", errors="replace").strip("\x00").strip()
                if text:
                    text_chunks.append(text)
            except UnicodeDecodeError:
                pass
        elif isinstance(value, str) and value.strip():
            text_chunks.append(value.strip())


def _try_decode_text(data: object, text_chunks: list[str]) -> None:
    """Try to decode raw bytes as text."""
    if not isinstance(data, bytes) or len(data) < 4:
        return
    # Try UTF-16LE first (common in OneNote)
    if len(data) % 2 == 0:
        try:
            text = data.decode("utf-16-le").strip("\x00").strip()
            if len(text) >= MIN_UTF16_STRING_CHARS:
                text_chunks.append(text)
                return
        except UnicodeDecodeError:
            pass
    # Try UTF-8/ASCII
    try:
        text = data.decode("utf-8", errors="strict").strip("\x00").strip()
        if len(text) >= MIN_UTF16_STRING_CHARS:
            text_chunks.append(text)
    except UnicodeDecodeError:
        pass


# ---------------------------------------------------------------------------
# Binary carving for embedded files
# ---------------------------------------------------------------------------

def _carve_embedded_files(raw_bytes: bytes) -> list[bytes]:
    """Carve FileDataStoreObjects from raw .one binary data.

    Searches for FDSO_HEADER markers and extracts embedded file content
    using the MS-ONESTORE structure (cbLength at offset+16).  Falls back
    to scanning for FDSO_FOOTER if the structured parse fails.
    """
    results: list[bytes] = []
    total_bytes = 0
    search_start = 0

    while True:
        idx = raw_bytes.find(FDSO_HEADER, search_start)
        if idx == -1:
            break

        search_start = idx + 16  # Advance past this header for next iteration
        carved: Optional[bytes] = None

        # Try structured parse: GUID(16) + cbLength(8) + unused(4) + reserved(8) + FileData
        meta_offset = idx + 16
        if meta_offset + 20 <= len(raw_bytes):
            try:
                cb_length = struct.unpack_from("<Q", raw_bytes, meta_offset)[0]
                data_offset = meta_offset + 20  # skip cbLength(8) + unused(4) + reserved(8)
                if (
                    cb_length > 0
                    and cb_length <= MAX_TOTAL_EMBEDDED_BYTES
                    and data_offset + cb_length <= len(raw_bytes)
                ):
                    file_data = raw_bytes[data_offset : data_offset + cb_length]
                    # Verify footer immediately follows
                    footer_offset = data_offset + cb_length
                    if (
                        footer_offset + 16 <= len(raw_bytes)
                        and raw_bytes[footer_offset : footer_offset + 16] == FDSO_FOOTER
                    ):
                        carved = file_data
                        search_start = footer_offset + 16
            except struct.error:
                pass

        # Fallback: scan for next footer and carve between
        if carved is None:
            footer_idx = raw_bytes.find(FDSO_FOOTER, idx + 16)
            if footer_idx != -1 and footer_idx - (idx + 36) > 0:
                # Strip 36 bytes of FDSO metadata (16 header + 8 cbLength + 4 unused + 8 reserved)
                data_start = idx + 36
                if data_start < footer_idx:
                    carved = raw_bytes[data_start:footer_idx]
                    search_start = footer_idx + 16

        if carved is None:
            continue

        # Skip recursive .one files
        if len(carved) >= 16 and carved[:16] == ONESTORE_HEADER:
            continue

        # Enforce cumulative size limit
        if total_bytes + len(carved) > MAX_TOTAL_EMBEDDED_BYTES:
            logger.warning(
                "Embedded file carving hit %d byte limit, stopping",
                MAX_TOTAL_EMBEDDED_BYTES,
            )
            break

        results.append(carved)
        total_bytes += len(carved)

    return results


# ---------------------------------------------------------------------------
# Embedded file text extraction
# ---------------------------------------------------------------------------

def _extract_embedded_file_text(
    content: bytes, temp_dir: str, index: int
) -> tuple[str, list[str]]:
    """Identify an embedded file by magic bytes, extract text if possible.

    Returns ``(extracted_text, warnings)``.
    """
    warnings: list[str] = []
    if not content:
        return "", warnings

    # Determine MIME type
    try:
        import magic

        mime_type = magic.from_buffer(content, mime=True)
    except Exception:
        mime_type = "application/octet-stream"

    ext = _MIME_TO_EXT.get(mime_type, "")
    if not ext:
        warnings.append(
            f"Embedded file {index}: unrecognized type {mime_type}, skipping text extraction"
        )
        return "", warnings

    # Write to temp file
    tmp_path = os.path.join(temp_dir, f"embed_{index}{ext}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)

        # Lazy import to avoid circular dependency at module load time
        from . import get_extractor

        extractor = get_extractor(ext)
        if extractor is None:
            warnings.append(
                f"Embedded file {index}: no extractor for {ext}"
            )
            return "", warnings

        result = extractor.extract(Path(tmp_path), len(content))
        if result.success and result.text_content:
            return result.text_content, warnings
        elif result.error:
            warnings.append(
                f"Embedded file {index} ({ext}): extraction failed: {result.error}"
            )
        return "", warnings
    except Exception as exc:
        warnings.append(
            f"Embedded file {index} ({ext}): error during extraction: {exc}"
        )
        return "", warnings
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Raw binary sweep fallback
# ---------------------------------------------------------------------------

def _raw_binary_sweep(raw_bytes: bytes) -> list[str]:
    """Brute-force UTF-16LE string extraction from raw .one bytes.

    Used as a fallback when pyOneNote fails or produces no text.
    Also carves embedded files for text extraction.
    """
    data = raw_bytes[:MAX_RAW_SWEEP_SIZE]
    segments: list[str] = []

    # Scan for UTF-16LE string runs
    i = 0
    current_chars: list[str] = []
    while i + 1 < len(data):
        lo, hi = data[i], data[i + 1]
        codepoint = lo | (hi << 8)

        is_printable = (
            (0x20 <= codepoint <= 0x7E)  # Basic ASCII printable
            or (0xA0 <= codepoint <= 0xFF)  # Extended Latin
            or codepoint in (0x09, 0x0A, 0x0D)  # Tab, newline, CR
        )

        if is_printable:
            current_chars.append(chr(codepoint))
        else:
            if len(current_chars) >= MIN_UTF16_STRING_CHARS:
                segments.append("".join(current_chars))
            current_chars = []

        i += 2

    # Flush last run
    if len(current_chars) >= MIN_UTF16_STRING_CHARS:
        segments.append("".join(current_chars))

    return segments


# ---------------------------------------------------------------------------
# Core extraction logic
# ---------------------------------------------------------------------------

def _extract_all(file_path: str) -> tuple[str, dict]:
    """Extract all text from a .one file.

    Returns ``(assembled_text, stats_dict)``.
    Raises ``ValueError`` for invalid files.
    """
    validate_onestore_header(file_path)

    with open(file_path, "rb") as f:
        raw_bytes = f.read()

    text_chunks: list[str] = []
    embedded_file_blobs: list[bytes] = []
    used_fallback = False

    # --- Primary: pyOneNote structured parsing ---
    try:
        pyon_chunks, pyon_embedded = _extract_via_pyonenote(file_path)
        text_chunks.extend(pyon_chunks)
        for emb in pyon_embedded:
            if isinstance(emb, dict) and emb.get("content"):
                embedded_file_blobs.append(emb["content"])
    except ImportError:
        logger.warning("pyOneNote not installed; falling back to raw binary sweep")
        used_fallback = True
    except Exception:
        logger.warning("pyOneNote parsing failed; falling back to raw binary sweep", exc_info=True)
        used_fallback = True

    # --- Fallback: raw binary sweep if pyOneNote failed or found no text ---
    if not text_chunks:
        used_fallback = True
        raw_segments = _raw_binary_sweep(raw_bytes)
        text_chunks.extend(raw_segments)

    # --- Carve embedded files (always, as supplement to pyOneNote) ---
    carved_blobs = _carve_embedded_files(raw_bytes)
    # Merge with any blobs pyOneNote already found (by content identity)
    existing_set = {id(b) for b in embedded_file_blobs}
    for blob in carved_blobs:
        # Simple dedup: skip if exact same bytes already captured
        if not any(blob == existing for existing in embedded_file_blobs):
            embedded_file_blobs.append(blob)

    # --- Extract text from embedded files ---
    embedded_texts: list[str] = []
    all_warnings: list[str] = []
    embedded_extracted_count = 0

    if embedded_file_blobs:
        temp_dir = tempfile.mkdtemp(prefix="onenote_embed_")
        try:
            for i, blob in enumerate(embedded_file_blobs):
                text, warnings = _extract_embedded_file_text(blob, temp_dir, i)
                all_warnings.extend(warnings)
                if text:
                    embedded_texts.append(text)
                    embedded_extracted_count += 1
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # --- Assemble output ---
    parts: list[str] = []
    if text_chunks:
        parts.append("--- OneNote Content ---")
        parts.append("\n\n".join(text_chunks))

    for i, emb_text in enumerate(embedded_texts):
        parts.append(f"\n--- Embedded File {i + 1} ---")
        parts.append(emb_text)

    assembled = "\n".join(parts)

    for w in all_warnings:
        logger.debug("OneNote extraction warning: %s", w)

    stats = {
        "text_chunks": len(text_chunks),
        "embedded_files_carved": len(embedded_file_blobs),
        "embedded_files_extracted": embedded_extracted_count,
        "used_fallback": used_fallback,
    }

    return assembled, stats


# ---------------------------------------------------------------------------
# Public standalone API
# ---------------------------------------------------------------------------

def extract_text_from_onenote(file_path: str) -> str:
    """Extract text from a OneNote .one section file.

    Returns the extracted text. Raises ``ValueError`` for invalid files;
    other exceptions propagate.
    """
    text, _stats = _extract_all(file_path)
    return text


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

class OnenoteExtractor(BaseExtractor):
    """Extract text from OneNote .one section files for the processing pipeline."""

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            text, stats = _extract_all(str(file_path))
        except ValueError as exc:
            return ExtractionResult(
                success=False,
                extraction_method="onenote_text",
                error=f"Invalid OneNote file: {exc}",
            )
        except Exception as exc:
            logger.exception("OneNote extraction failed for %s", file_path)
            return ExtractionResult(
                success=False,
                extraction_method="onenote_text",
                error=str(exc),
            )

        stripped = text.strip()
        if len(stripped) < self.MIN_MEANINGFUL_TEXT:
            return ExtractionResult(
                success=False,
                extraction_method="onenote_text",
                metadata=stats,
                error="OneNote extraction produced insufficient text",
                warnings=["Text extraction produced < 50 characters"],
            )

        text, was_sampled, description = self._truncate_if_needed(
            text,
            f"First ~100KB of text from OneNote section "
            f"({stats['text_chunks']} chunks, "
            f"{stats['embedded_files_extracted']} embedded files)",
        )

        return ExtractionResult(
            success=True,
            extraction_method="onenote_text",
            text_content=text,
            metadata=stats,
            was_sampled=was_sampled,
            sampling_description=description,
            content_length=len(text),
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m app.extraction.onenote_extractor <file.one>", file=sys.stderr)
        sys.exit(1)

    target = sys.argv[1]
    try:
        result_text, result_stats = _extract_all(target)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(result_text)
    print("\n--- Summary ---", file=sys.stderr)
    print(f"Text chunks extracted: {result_stats['text_chunks']}", file=sys.stderr)
    print(f"Embedded files carved: {result_stats['embedded_files_carved']}", file=sys.stderr)
    print(
        f"Text extracted from embedded files: {result_stats['embedded_files_extracted']}",
        file=sys.stderr,
    )
    print(
        f"Used raw binary fallback: {'yes' if result_stats['used_fallback'] else 'no'}",
        file=sys.stderr,
    )
    print(f"Total character count: {len(result_text)}", file=sys.stderr)
