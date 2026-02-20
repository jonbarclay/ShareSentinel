# 04 - Text Extraction Module

## Purpose

The text extraction module converts downloaded files into text content that can be sent to the AI for analysis. The goal is to extract meaningful text from every processable file type, applying sampling strategies when content exceeds size limits. Text-based AI analysis is significantly cheaper than multimodal analysis, so this module's success directly impacts API costs.

## Design Principles

1. **Always attempt text extraction first**, even for file types that support multimodal analysis (like PDFs). Text-based AI calls are cheaper and often more accurate for document analysis.
2. **Extract document metadata alongside content**. Properties like title, author, subject, comments, and sheet names provide valuable context for sensitivity analysis.
3. **Sample large content deterministically**. When content exceeds the 100KB limit, apply consistent, repeatable sampling so the same file always produces the same sample.
4. **Fail gracefully**. If text extraction produces garbage or fails, return a clear signal so the pipeline can fall back to OCR or multimodal analysis.

## Base Extractor Interface

All extractors implement a common interface:

```python
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from abc import ABC, abstractmethod
from pathlib import Path

@dataclass
class ExtractionResult:
    success: bool
    extraction_method: str              # "pdf_text", "docx_text", "xlsx_text", "ocr", etc.
    text_content: Optional[str] = None  # The extracted text
    metadata: Dict = field(default_factory=dict)  # Document properties, sheet names, etc.
    was_sampled: bool = False
    sampling_description: str = ""      # e.g., "First 500 rows of 50,000 total rows"
    content_length: int = 0             # Length of extracted text in characters
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None         # Error message if extraction failed

class BaseExtractor(ABC):
    """Base class for all text extractors."""
    
    MAX_TEXT_SIZE = 100_000  # 100KB character limit (~25K tokens)
    MIN_MEANINGFUL_TEXT = 50  # Minimum characters to consider extraction successful
    
    @abstractmethod
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        """Extract text content from the file."""
        pass
    
    def _truncate_if_needed(self, text: str, description: str) -> tuple[str, bool, str]:
        """Truncate text to MAX_TEXT_SIZE if needed. Returns (text, was_sampled, description)."""
        if len(text) <= self.MAX_TEXT_SIZE:
            return text, False, ""
        truncated = text[:self.MAX_TEXT_SIZE]
        return truncated, True, description
```

## PDF Extractor

**Library**: PyMuPDF (fitz)

**Strategy**: Extract text from all pages. PyMuPDF produces clean text from native PDFs (PDFs with embedded text layers). For scanned PDFs (image-only), PyMuPDF will return little or no text, which triggers the OCR fallback in the pipeline.

```python
import fitz  # PyMuPDF

class PDFExtractor(BaseExtractor):
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
            
            all_text = []
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
                    warnings=["Text extraction produced < 50 characters; likely a scanned document"]
                )
            
            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"First ~100KB of text from a {metadata['page_count']}-page PDF"
            )
            
            return ExtractionResult(
                success=True,
                extraction_method="pdf_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=description,
                content_length=len(text)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="pdf_text",
                error=str(e)
            )
```

## Word Document Extractor (.docx)

**Library**: python-docx

**Strategy**: Extract body text, header/footer text, and comment text. Also extract document properties (title, author, subject, keywords, comments from metadata). Concatenate all text sources with clear section markers.

```python
from docx import Document

class DocxExtractor(BaseExtractor):
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            doc = Document(str(file_path))
            
            # Document properties
            props = doc.core_properties
            metadata = {
                "title": props.title or "",
                "author": props.author or "",
                "subject": props.subject or "",
                "keywords": props.keywords or "",
                "comments": props.comments or "",
                "last_modified_by": props.last_modified_by or "",
                "category": props.category or "",
            }
            
            parts = []
            
            # Document properties as context
            prop_text = []
            if metadata["title"]:
                prop_text.append(f"Document Title: {metadata['title']}")
            if metadata["author"]:
                prop_text.append(f"Author: {metadata['author']}")
            if metadata["subject"]:
                prop_text.append(f"Subject: {metadata['subject']}")
            if metadata["keywords"]:
                prop_text.append(f"Keywords: {metadata['keywords']}")
            if prop_text:
                parts.append("--- Document Properties ---\n" + "\n".join(prop_text))
            
            # Body text
            body_paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            if body_paragraphs:
                parts.append("--- Document Body ---\n" + "\n".join(body_paragraphs))
            
            # Table content
            for i, table in enumerate(doc.tables):
                rows_text = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows_text.append(" | ".join(cells))
                if rows_text:
                    parts.append(f"--- Table {i+1} ---\n" + "\n".join(rows_text))
            
            # Headers and footers
            header_footer_text = []
            for section in doc.sections:
                if section.header and section.header.paragraphs:
                    for p in section.header.paragraphs:
                        if p.text.strip():
                            header_footer_text.append(f"Header: {p.text.strip()}")
                if section.footer and section.footer.paragraphs:
                    for p in section.footer.paragraphs:
                        if p.text.strip():
                            header_footer_text.append(f"Footer: {p.text.strip()}")
            if header_footer_text:
                parts.append("--- Headers/Footers ---\n" + "\n".join(header_footer_text))
            
            # Comments (tracked changes comments)
            # Note: python-docx doesn't natively support comment extraction.
            # For MVP, skip comments. Can add later using lxml to parse the XML directly.
            
            full_text = "\n\n".join(parts)
            
            if len(full_text.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="docx_text",
                    metadata=metadata,
                    error="Document appears to be empty or contains only non-text elements"
                )
            
            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"First ~100KB of text from Word document"
            )
            
            return ExtractionResult(
                success=True,
                extraction_method="docx_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=description,
                content_length=len(text)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="docx_text",
                error=str(e)
            )
```

## Excel Extractor (.xlsx)

**Library**: openpyxl

**Strategy**: This is the most nuanced extractor. Excel workbooks can have multiple sheets, and sensitive data might be on any sheet. The strategy is to extract sheet names first (they're often descriptive), then extract the first 200 rows from each of the first 10 sheets. Sheet names alone can be highly informative ("Employee SSNs", "Salary Data", "Medical Records").

**Sampling rules for large workbooks**:
- Extract ALL sheet names (include them in the output regardless of how many sheets there are).
- For each sheet (up to the first 10 sheets), extract the header row + first 200 data rows.
- If a sheet has more than 200 rows, note the total row count in the output.
- If there are more than 10 sheets, list the remaining sheet names but don't extract their content.
- After extraction, if total text exceeds 100KB, further truncate by reducing rows per sheet.

```python
from openpyxl import load_workbook

class XlsxExtractor(BaseExtractor):
    MAX_SHEETS_TO_EXTRACT = 10
    MAX_ROWS_PER_SHEET = 200
    
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            # read_only=True for memory efficiency with large files
            # data_only=True to get computed values, not formulas
            wb = load_workbook(str(file_path), read_only=True, data_only=True)
            
            sheet_names = wb.sheetnames
            metadata = {
                "sheet_count": len(sheet_names),
                "sheet_names": sheet_names,
            }
            
            parts = []
            parts.append(f"Workbook contains {len(sheet_names)} sheet(s): {', '.join(sheet_names)}")
            
            for idx, sheet_name in enumerate(sheet_names):
                if idx >= self.MAX_SHEETS_TO_EXTRACT:
                    remaining = sheet_names[idx:]
                    parts.append(f"\n--- Additional sheets not extracted ({len(remaining)}): {', '.join(remaining)} ---")
                    break
                
                ws = wb[sheet_name]
                rows = []
                total_rows = 0
                
                for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
                    total_rows += 1
                    if row_idx < self.MAX_ROWS_PER_SHEET:
                        # Convert cell values to strings, handling None
                        cells = [str(cell) if cell is not None else "" for cell in row]
                        rows.append(" | ".join(cells))
                
                sheet_header = f"\n--- Sheet: {sheet_name} ({total_rows} total rows)"
                if total_rows > self.MAX_ROWS_PER_SHEET:
                    sheet_header += f", showing first {self.MAX_ROWS_PER_SHEET}"
                sheet_header += " ---"
                
                parts.append(sheet_header)
                if rows:
                    parts.append("\n".join(rows))
                else:
                    parts.append("(empty sheet)")
            
            wb.close()
            
            full_text = "\n".join(parts)
            
            if len(full_text.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="xlsx_text",
                    metadata=metadata,
                    error="Workbook appears to be empty"
                )
            
            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"First {self.MAX_ROWS_PER_SHEET} rows from each of up to {self.MAX_SHEETS_TO_EXTRACT} sheets in a {len(sheet_names)}-sheet workbook"
            )
            
            return ExtractionResult(
                success=True,
                extraction_method="xlsx_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled or any(True for _ in sheet_names[self.MAX_SHEETS_TO_EXTRACT:]),
                sampling_description=description,
                content_length=len(text)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="xlsx_text",
                error=str(e)
            )
```

## PowerPoint Extractor (.pptx)

**Library**: python-pptx

**Strategy**: Extract text from all slides, including text boxes, tables, and speaker notes. Speaker notes are especially important because people often put sensitive information there that they don't realize would be included when sharing the file.

```python
from pptx import Presentation

class PptxExtractor(BaseExtractor):
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            prs = Presentation(str(file_path))
            
            metadata = {
                "slide_count": len(prs.slides),
            }
            # Extract core properties if available
            try:
                props = prs.core_properties
                metadata["title"] = props.title or ""
                metadata["author"] = props.author or ""
                metadata["subject"] = props.subject or ""
                metadata["keywords"] = props.keywords or ""
            except Exception:
                pass  # Properties might not be accessible
            
            parts = []
            
            for slide_num, slide in enumerate(prs.slides, 1):
                slide_parts = []
                
                # Slide text from shapes
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for paragraph in shape.text_frame.paragraphs:
                            text = paragraph.text.strip()
                            if text:
                                slide_parts.append(text)
                    
                    # Table content
                    if shape.has_table:
                        for row in shape.table.rows:
                            cells = [cell.text.strip() for cell in row.cells]
                            slide_parts.append(" | ".join(cells))
                
                # Speaker notes
                notes_text = ""
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                    notes_text = slide.notes_slide.notes_text_frame.text.strip()
                
                slide_header = f"--- Slide {slide_num} ---"
                if slide_parts:
                    parts.append(slide_header + "\n" + "\n".join(slide_parts))
                
                if notes_text:
                    parts.append(f"--- Slide {slide_num} Speaker Notes ---\n{notes_text}")
            
            full_text = "\n\n".join(parts)
            
            if len(full_text.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="pptx_text",
                    metadata=metadata,
                    error="Presentation appears to be empty or contains only non-text elements"
                )
            
            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"First ~100KB of text from a {metadata['slide_count']}-slide presentation"
            )
            
            return ExtractionResult(
                success=True,
                extraction_method="pptx_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=description,
                content_length=len(text)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="pptx_text",
                error=str(e)
            )
```

## CSV/TSV Extractor

**Library**: Built-in `csv` module

**Strategy**: Read the header row plus the first 500 data rows. CSVs are ideal for sampling because sensitive data (if present) is usually structurally consistent across rows. If the first 500 rows contain SSNs, the remaining rows almost certainly do too.

```python
import csv

class CsvExtractor(BaseExtractor):
    MAX_ROWS = 500
    
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            # Detect delimiter
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                sample = f.read(4096)
                sniffer = csv.Sniffer()
                try:
                    dialect = sniffer.sniff(sample)
                    delimiter = dialect.delimiter
                except csv.Error:
                    delimiter = ',' if file_path.suffix.lower() == '.csv' else '\t'
            
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                reader = csv.reader(f, delimiter=delimiter)
                rows = []
                total_rows = 0
                
                for i, row in enumerate(reader):
                    total_rows += 1
                    if i <= self.MAX_ROWS:  # header + 500 data rows
                        rows.append(" | ".join(row))
            
            metadata = {
                "total_rows": total_rows,
                "rows_extracted": min(total_rows, self.MAX_ROWS + 1),
                "delimiter": delimiter,
            }
            
            header = f"CSV/TSV file with {total_rows} total rows"
            if total_rows > self.MAX_ROWS + 1:
                header += f" (showing header + first {self.MAX_ROWS} data rows)"
            
            full_text = header + "\n\n" + "\n".join(rows)
            
            if len(full_text.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="csv_text",
                    metadata=metadata,
                    error="File appears to be empty"
                )
            
            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"Header + first {self.MAX_ROWS} rows of {total_rows} total rows"
            )
            
            return ExtractionResult(
                success=True,
                extraction_method="csv_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled or total_rows > self.MAX_ROWS + 1,
                sampling_description=description if was_sampled else (
                    f"First {self.MAX_ROWS} of {total_rows} rows" if total_rows > self.MAX_ROWS + 1 else ""
                ),
                content_length=len(text)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="csv_text",
                error=str(e)
            )
```

## Plain Text Extractor (.txt, .log, .md, .json, .xml, .html, .rtf)

**Library**: Built-in file I/O

**Strategy**: Read the first 100KB of the file. For structured formats (JSON, XML, HTML), read the raw text rather than parsing the structure. The AI can understand these formats from raw text. For RTF, strip the RTF control codes if possible, or send the raw RTF (the AI can usually interpret it).

```python
class TextExtractor(BaseExtractor):
    MAX_READ_BYTES = 100_000  # 100KB
    
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            # Try UTF-8 first, fall back to latin-1
            encoding = 'utf-8'
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read(self.MAX_READ_BYTES)
            except UnicodeDecodeError:
                encoding = 'latin-1'
                with open(file_path, 'r', encoding='latin-1') as f:
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
                    error="File appears to be empty or contains very little text"
                )
            
            return ExtractionResult(
                success=True,
                extraction_method="text_direct",
                text_content=content,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=f"First 100KB of a {file_size / 1024:.0f}KB file" if was_sampled else "",
                content_length=len(content)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="text_direct",
                error=str(e)
            )
```

## Archive Manifest Extractor (.zip, .rar, .7z)

**Library**: Built-in `zipfile` module (for .zip), `rarfile` (for .rar), `py7zr` (for .7z)

**Strategy**: List internal filenames and sizes WITHOUT extracting any content. The AI analyzes just the manifest (file listing) for suspicious filenames.

```python
import zipfile

class ArchiveExtractor(BaseExtractor):
    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        ext = file_path.suffix.lower()
        
        try:
            if ext == '.zip':
                return self._extract_zip(file_path, file_size)
            else:
                # For non-zip archives, just report the file type
                return ExtractionResult(
                    success=True,
                    extraction_method="archive_manifest",
                    text_content=f"Archive file ({ext}) containing unknown contents. Filename: {file_path.name}",
                    metadata={"archive_type": ext},
                    content_length=0,
                    warnings=[f"Archive type {ext} manifest extraction not implemented; using filename only"]
                )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="archive_manifest",
                error=str(e)
            )
    
    def _extract_zip(self, file_path: Path, file_size: int) -> ExtractionResult:
        with zipfile.ZipFile(str(file_path), 'r') as zf:
            file_list = zf.namelist()
            info_list = zf.infolist()
            
            metadata = {
                "total_files": len(file_list),
                "archive_type": "zip",
            }
            
            parts = [f"ZIP archive containing {len(file_list)} files/directories:\n"]
            
            for info in info_list:
                size_str = f" ({info.file_size:,} bytes)" if info.file_size > 0 else " (directory)"
                parts.append(f"  {info.filename}{size_str}")
            
            full_text = "\n".join(parts)
            
            return ExtractionResult(
                success=True,
                extraction_method="archive_manifest",
                text_content=full_text,
                metadata=metadata,
                content_length=len(full_text)
            )
```

## OCR Extractor (Tesseract Fallback)

**Library**: pytesseract + Tesseract OCR engine + PyMuPDF (for PDF page rendering)

**Strategy**: Used when PDF text extraction fails (scanned documents). Render each PDF page as an image, then run Tesseract OCR on each page image. Limit to the first 5 pages to keep processing time reasonable.

```python
import fitz  # PyMuPDF for rendering
import pytesseract
from PIL import Image
import io

class OcrExtractor(BaseExtractor):
    MAX_PAGES = 5
    RENDER_DPI = 150  # Lower DPI = smaller images = faster OCR; 150 is sufficient for text
    
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
            
            parts = []
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
                    parts.append(f"--- Page {page_num + 1} (OCR) ---\n{ocr_text.strip()}")
            
            doc.close()
            
            full_text = "\n\n".join(parts)
            
            if len(full_text.strip()) < self.MIN_MEANINGFUL_TEXT:
                return ExtractionResult(
                    success=False,
                    extraction_method="ocr",
                    metadata=metadata,
                    error="OCR produced insufficient text (document may be handwritten, low quality, or non-text content)",
                    warnings=["OCR extracted < 50 characters; multimodal analysis recommended"]
                )
            
            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"OCR text from first {pages_to_process} of {page_count} pages"
            )
            
            return ExtractionResult(
                success=True,
                extraction_method="ocr",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled or pages_to_process < page_count,
                sampling_description=description if was_sampled else (
                    f"OCR of first {pages_to_process} of {page_count} pages" if pages_to_process < page_count else ""
                ),
                content_length=len(text)
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="ocr",
                error=str(e)
            )
```

## Extractor Factory

A factory function that returns the appropriate extractor based on file extension:

```python
def get_extractor(file_extension: str) -> Optional[BaseExtractor]:
    """Return the appropriate extractor for the given file extension."""
    extractors = {
        '.pdf': PDFExtractor(),
        '.docx': DocxExtractor(),
        '.doc': DocxExtractor(),
        '.xlsx': XlsxExtractor(),
        '.xls': XlsxExtractor(),
        '.pptx': PptxExtractor(),
        '.ppt': PptxExtractor(),
        '.csv': CsvExtractor(),
        '.tsv': CsvExtractor(),
        '.txt': TextExtractor(),
        '.log': TextExtractor(),
        '.md': TextExtractor(),
        '.json': TextExtractor(),
        '.xml': TextExtractor(),
        '.html': TextExtractor(),
        '.htm': TextExtractor(),
        '.rtf': TextExtractor(),
        '.zip': ArchiveExtractor(),
        '.rar': ArchiveExtractor(),
        '.7z': ArchiveExtractor(),
    }
    return extractors.get(file_extension.lower())
```

## Python Dependencies for Extraction

Add the following to `services/worker/requirements.txt`:

```
PyMuPDF>=1.23.0          # PDF text extraction and page rendering
python-docx>=0.8.11      # Word document extraction
openpyxl>=3.1.0           # Excel extraction
python-pptx>=0.6.21      # PowerPoint extraction
pytesseract>=0.3.10      # Tesseract OCR wrapper
Pillow>=10.0.0           # Image processing (used by OCR and image preprocessing)
```

The Docker image for the worker must also install Tesseract:
```dockerfile
RUN apt-get update && apt-get install -y tesseract-ocr && rm -rf /var/lib/apt/lists/*
```

## Edge Cases and Known Limitations

1. **Password-protected files**: openpyxl and python-docx cannot open password-protected files. The extraction will fail, and the pipeline should record this as "password_protected" and flag for analyst review (a password-protected file shared anonymously is itself suspicious).

2. **Corrupted files**: Any extraction library can throw unexpected exceptions on corrupted files. The base try/except in each extractor handles this, returning an extraction failure that the pipeline can act on.

3. **Very wide spreadsheets**: Excel files with hundreds of columns will produce very long row strings. The 100KB overall text limit handles this, but individual rows might be cut off. This is acceptable; the AI only needs a representative sample.

4. **Embedded objects in Office documents**: Word, Excel, and PowerPoint files can contain embedded OLE objects (other files inside the document). The standard extraction libraries do not extract content from embedded objects. This is a known limitation; log it as a warning when embedded objects are detected.

5. **Macro-enabled files (.xlsm, .docm, .pptm)**: These can be processed with the same libraries as their non-macro counterparts. The macros themselves are not extracted (and generally don't contain sensitive PII). Treat these the same as their regular counterparts.

6. **Legacy formats (.doc, .xls, .ppt)**: python-docx can handle some .doc files, but not all. openpyxl handles .xls via xlrd compatibility in some cases. For legacy formats that fail extraction, fall back to filename/path analysis and log the limitation.
