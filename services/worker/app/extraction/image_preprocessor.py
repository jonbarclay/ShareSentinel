"""Image preprocessing for multimodal AI analysis.

Resizes and compresses images to minimize API costs while retaining enough
quality for the AI to read text and understand visual content.  Also renders
scanned PDF pages as images when text extraction and OCR both fail.
"""

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PIL import Image

logger = logging.getLogger(__name__)

# Register HEIC support if available
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIC_SUPPORTED = True
except ImportError:
    _HEIC_SUPPORTED = False
    logger.debug("pillow-heif not installed; HEIC support disabled")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DIMENSION = 1600
JPEG_QUALITY = 85
MAX_IMAGE_SIZE_BYTES = 1_000_000  # 1 MB target
MAX_PAGES_FOR_MULTIMODAL = 3
RENDER_DPI = 150


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PreprocessedImage:
    """Result of preprocessing a single image."""

    image_bytes: bytes
    mime_type: str  # "image/jpeg" or "image/png"
    original_width: int
    original_height: int
    processed_width: int
    processed_height: int
    original_size_bytes: int
    processed_size_bytes: int
    source: str  # "direct_image", "pdf_page_1_of_12", etc.


@dataclass
class MultimodalContent:
    """Content package for multimodal AI analysis."""

    images: List[PreprocessedImage]
    context_text: str
    total_pages: Optional[int] = None
    pages_included: Optional[int] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preprocess_image(
    file_path: Path,
    source_label: str = "direct_image",
) -> PreprocessedImage:
    """Resize and compress an image for multimodal AI analysis.

    Parameters
    ----------
    file_path:
        Path to the source image file.
    source_label:
        Label describing the image origin (e.g. ``"direct_image"``,
        ``"pdf_page_1_of_12"``).
    """
    original_size = file_path.stat().st_size

    img = Image.open(file_path)
    original_width, original_height = img.size

    # Convert unsupported colour modes to RGB
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

    # Animated GIFs / multi-frame TIFFs: take only the first frame
    if hasattr(img, "n_frames") and img.n_frames > 1:
        img.seek(0)

    # Resize if any dimension exceeds MAX_DIMENSION
    if original_width > MAX_DIMENSION or original_height > MAX_DIMENSION:
        ratio = min(MAX_DIMENSION / original_width, MAX_DIMENSION / original_height)
        new_width = int(original_width * ratio)
        new_height = int(original_height * ratio)
        img = img.resize((new_width, new_height), Image.LANCZOS)
    else:
        new_width, new_height = original_width, original_height

    # Determine output format: PNG for transparency, JPEG otherwise
    if img.mode == "RGBA":
        output_format = "PNG"
        mime_type = "image/png"
    else:
        output_format = "JPEG"
        mime_type = "image/jpeg"
        if img.mode != "RGB":
            img = img.convert("RGB")

    # Compress to bytes
    image_bytes = _save_to_bytes(img, output_format, JPEG_QUALITY)

    # Progressive quality reduction for oversized JPEGs
    if len(image_bytes) > MAX_IMAGE_SIZE_BYTES and output_format == "JPEG":
        for quality in (70, 55, 40):
            image_bytes = _save_to_bytes(img, "JPEG", quality)
            if len(image_bytes) <= MAX_IMAGE_SIZE_BYTES:
                break

    # Last resort: reduce dimensions further
    if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
        shrink = 0.7
        new_width = int(new_width * shrink)
        new_height = int(new_height * shrink)
        img = img.resize((new_width, new_height), Image.LANCZOS)
        image_bytes = _save_to_bytes(img, output_format, JPEG_QUALITY)

    return PreprocessedImage(
        image_bytes=image_bytes,
        mime_type=mime_type,
        original_width=original_width,
        original_height=original_height,
        processed_width=new_width,
        processed_height=new_height,
        original_size_bytes=original_size,
        processed_size_bytes=len(image_bytes),
        source=source_label,
    )


def render_pdf_pages_as_images(file_path: Path) -> List[PreprocessedImage]:
    """Render the first pages of a PDF as images for multimodal analysis.

    Used when text extraction and OCR have both failed on a scanned PDF.
    Each rendered page is passed through :func:`preprocess_image`.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(file_path))
    page_count = doc.page_count
    pages_to_render = min(page_count, MAX_PAGES_FOR_MULTIMODAL)

    images: List[PreprocessedImage] = []
    for page_num in range(pages_to_render):
        page = doc[page_num]
        mat = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")

        # Write to a temp file so the standard preprocessor can handle it
        temp_img_path = file_path.parent / f"_page_{page_num + 1}.png"
        try:
            temp_img_path.write_bytes(img_bytes)
            preprocessed = preprocess_image(
                temp_img_path,
                source_label=f"pdf_page_{page_num + 1}_of_{page_count}",
            )
            images.append(preprocessed)
        finally:
            temp_img_path.unlink(missing_ok=True)

    doc.close()
    return images


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_to_bytes(img: Image.Image, fmt: str, quality: int) -> bytes:
    """Save a PIL Image to bytes in the given format."""
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=quality, optimize=True)
    else:
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
