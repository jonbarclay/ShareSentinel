"""Archive manifest extraction for ZIP (and stub for other archive types)."""

import zipfile
from pathlib import Path

from .base import BaseExtractor, ExtractionResult


class ArchiveExtractor(BaseExtractor):
    """List archive contents (filenames and sizes) without extracting files."""

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        ext = file_path.suffix.lower()

        try:
            if ext == ".zip":
                return self._extract_zip(file_path, file_size)
            else:
                # For non-zip archives, just report the file type
                return ExtractionResult(
                    success=True,
                    extraction_method="archive_manifest",
                    text_content=(
                        f"Archive file ({ext}) containing unknown contents. "
                        f"Filename: {file_path.name}"
                    ),
                    metadata={"archive_type": ext},
                    content_length=0,
                    warnings=[
                        f"Archive type {ext} manifest extraction not implemented; "
                        f"using filename only"
                    ],
                )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="archive_manifest",
                error=str(e),
            )

    def _extract_zip(self, file_path: Path, file_size: int) -> ExtractionResult:
        with zipfile.ZipFile(str(file_path), "r") as zf:
            file_list = zf.namelist()
            info_list = zf.infolist()

            metadata = {
                "total_files": len(file_list),
                "archive_type": "zip",
            }

            parts: list[str] = [
                f"ZIP archive containing {len(file_list)} files/directories:\n"
            ]

            for info in info_list:
                size_str = (
                    f" ({info.file_size:,} bytes)"
                    if info.file_size > 0
                    else " (directory)"
                )
                parts.append(f"  {info.filename}{size_str}")

            full_text = "\n".join(parts)

            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"First ~100KB of manifest from a {len(file_list)}-entry ZIP archive",
            )

            return ExtractionResult(
                success=True,
                extraction_method="archive_manifest",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled,
                sampling_description=description,
                content_length=len(text),
            )
