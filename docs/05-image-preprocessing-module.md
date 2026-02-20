# 05 - Image Preprocessing Module

## Purpose

The image preprocessing module prepares image files for multimodal AI analysis. This module is used for two categories of files:

1. **Actual image files** (.png, .jpg, .jpeg, .tiff, .bmp, .gif, .webp, .heic) that were directly shared.
2. **Scanned PDFs** where text extraction and OCR both failed, requiring the pages to be sent as images for visual analysis.

The goal is to produce images that are small enough to minimize API costs while retaining enough quality for the AI to read any text and understand any visual content in the image.

## Image Resizing Strategy

AI APIs charge based on image dimensions (token count scales with pixel count). Reducing image dimensions directly reduces API costs. For document analysis, the AI needs to read text and understand layout, but does NOT need pixel-perfect resolution.

**Target specifications:**
- Maximum longest edge: 1600 pixels
- Output format: JPEG (quality 85) for photos and scanned documents; PNG for screenshots and diagrams with sharp text
- Target file size: Under 1MB per image
- Color: Maintain original color space (do not convert to grayscale; some documents use color coding that could be relevant)

**Decision logic:**

```python
from PIL import Image
import io
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class PreprocessedImage:
    image_bytes: bytes
    mime_type: str          # "image/jpeg" or "image/png"
    original_width: int
    original_height: int
    processed_width: int
    processed_height: int
    original_size_bytes: int
    processed_size_bytes: int
    source: str             # "direct_image", "pdf_page_1", etc.

MAX_DIMENSION = 1600
JPEG_QUALITY = 85
MAX_IMAGE_SIZE_BYTES = 1_000_000  # 1MB target

def preprocess_image(file_path: Path, source_label: str = "direct_image") -> PreprocessedImage:
    """
    Resize and compress an image for multimodal AI analysis.
    """
    original_size = file_path.stat().st_size
    
    img = Image.open(file_path)
    original_width, original_height = img.size
    
    # Handle HEIC, TIFF, BMP by converting to RGB
    if img.mode not in ('RGB', 'RGBA', 'L'):
        img = img.convert('RGB')
    
    # Handle animated GIFs: just take the first frame
    if hasattr(img, 'n_frames') and img.n_frames > 1:
        img.seek(0)
    
    # Resize if either dimension exceeds MAX_DIMENSION
    if original_width > MAX_DIMENSION or original_height > MAX_DIMENSION:
        ratio = min(MAX_DIMENSION / original_width, MAX_DIMENSION / original_height)
        new_width = int(original_width * ratio)
        new_height = int(original_height * ratio)
        img = img.resize((new_width, new_height), Image.LANCZOS)
    else:
        new_width, new_height = original_width, original_height
    
    # Determine output format
    # Use PNG for images with transparency or very sharp edges (screenshots)
    # Use JPEG for everything else (photos, scanned documents)
    if img.mode == 'RGBA':
        output_format = 'PNG'
        mime_type = 'image/png'
    else:
        output_format = 'JPEG'
        mime_type = 'image/jpeg'
        if img.mode == 'RGBA':
            img = img.convert('RGB')
    
    # Compress to bytes
    buffer = io.BytesIO()
    if output_format == 'JPEG':
        img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    else:
        img.save(buffer, format='PNG', optimize=True)
    
    image_bytes = buffer.getvalue()
    
    # If still over 1MB, reduce quality further for JPEG
    if len(image_bytes) > MAX_IMAGE_SIZE_BYTES and output_format == 'JPEG':
        for quality in [70, 55, 40]:
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            image_bytes = buffer.getvalue()
            if len(image_bytes) <= MAX_IMAGE_SIZE_BYTES:
                break
    
    # If still over 1MB (unlikely at this point), reduce dimensions further
    if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
        ratio = 0.7
        new_width = int(new_width * ratio)
        new_height = int(new_height * ratio)
        img = img.resize((new_width, new_height), Image.LANCZOS)
        buffer = io.BytesIO()
        if output_format == 'JPEG':
            img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
        else:
            img.save(buffer, format='PNG', optimize=True)
        image_bytes = buffer.getvalue()
    
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
```

## Scanned PDF Page Rendering

When a PDF fails text extraction and OCR, the pipeline extracts individual pages as images for multimodal analysis. This function renders PDF pages to images using PyMuPDF.

**Rendering specifications:**
- DPI: 150 (sufficient for AI text reading; 72 is too low, 300 is wastefully high)
- Maximum pages to render: 3 (balances coverage against API cost)
- Each rendered page is then processed through the image preprocessor above

```python
import fitz  # PyMuPDF

MAX_PAGES_FOR_MULTIMODAL = 3
RENDER_DPI = 150

def render_pdf_pages_as_images(file_path: Path) -> List[PreprocessedImage]:
    """
    Render PDF pages as images for multimodal AI analysis.
    Used when text extraction and OCR have both failed.
    """
    doc = fitz.open(str(file_path))
    page_count = doc.page_count
    pages_to_render = min(page_count, MAX_PAGES_FOR_MULTIMODAL)
    
    images = []
    for page_num in range(pages_to_render):
        page = doc[page_num]
        
        # Render page at specified DPI
        mat = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        
        # Convert to PIL Image
        img_bytes = pix.tobytes("png")
        
        # Save to a temporary file for the preprocessor
        # (or refactor preprocess_image to accept bytes directly)
        temp_img_path = file_path.parent / f"page_{page_num + 1}.png"
        with open(temp_img_path, 'wb') as f:
            f.write(img_bytes)
        
        # Process through the standard image preprocessor
        preprocessed = preprocess_image(
            temp_img_path,
            source_label=f"pdf_page_{page_num + 1}_of_{page_count}"
        )
        images.append(preprocessed)
        
        # Clean up temp page image
        temp_img_path.unlink(missing_ok=True)
    
    doc.close()
    return images
```

## Integration with AI Analysis

When sending preprocessed images to the AI, the module produces a package that the AI provider abstraction layer can consume:

```python
@dataclass
class MultimodalContent:
    """Content package for multimodal AI analysis."""
    images: List[PreprocessedImage]
    context_text: str              # Textual context to include with the images
    total_pages: Optional[int]     # For PDFs: total page count
    pages_included: Optional[int]  # For PDFs: how many pages are included as images
```

The `context_text` includes:
- File name and path
- File size
- Who shared it and when
- Sharing type and permissions
- Number of pages/images included vs. total
- Any filename sensitivity keyword flags

Example context text sent alongside images:
```
File: tax_return_2024.pdf
Path: /personal/jsmith/Documents/Financial/
Size: 4.2 MB (12 pages total, showing first 3 as images)
Shared by: jsmith@organization.com
Sharing: Anonymous link with View access
Created: 2024-01-15T10:30:00Z
Note: Filename contains sensitivity keyword match: "tax"

Please analyze the following page images from this document for sensitive content.
```

## Cost Estimation

For reference, approximate token costs for images at different sizes (these vary by provider and may change):

**Anthropic Claude:**
- Images are encoded as tokens based on dimensions
- A 1600x1200 image ≈ ~1,600 tokens
- A 800x600 image ≈ ~800 tokens

**OpenAI GPT-4 Vision:**
- Uses a tile-based system (512x512 tiles)
- A 1600x1200 image = 12 tiles ≈ ~2,040 tokens
- A 800x600 image = 4 tiles ≈ ~680 tokens

**Google Gemini:**
- Fixed cost per image regardless of size (258 tokens per image as of early 2024)

By resizing images to a max of 1600px on the longest edge, we keep costs reasonable across all three providers. The text extraction path (when successful) is always cheaper, typically 10-50x cheaper per file than multimodal analysis.

## HEIC Handling

HEIC (Apple's image format) requires the `pillow-heif` package:

```
pip install pillow-heif
```

And registration in the code:
```python
from pillow_heif import register_heif_opener
register_heif_opener()
# Now Pillow can open .heic files
```

This should be done once at module initialization.

## Edge Cases

1. **Animated GIFs**: Only process the first frame. An animated GIF is very unlikely to contain sensitive document content.

2. **Very small images** (under 100x100 pixels): These are likely icons or thumbnails, not sensitive documents. Process them anyway (the AI will quickly determine they're not sensitive), but note the small dimensions in the context text.

3. **Multi-page TIFF**: TIFF files can contain multiple pages (like a fax). Process the first 3 pages, similar to the scanned PDF strategy. Use `img.n_frames` to detect multi-page TIFFs and iterate with `img.seek(frame_number)`.

4. **Corrupt or unreadable images**: Pillow will throw an exception if the image is corrupt. The extraction result should indicate failure, and the pipeline falls back to filename/path analysis.
