"""CSV/TSV text extraction using the built-in csv module."""

import csv
from pathlib import Path

from .base import BaseExtractor, ExtractionResult


class CsvExtractor(BaseExtractor):
    """Extract text from CSV/TSV files: header + first 500 data rows."""

    MAX_ROWS = 500

    def extract(self, file_path: Path, file_size: int) -> ExtractionResult:
        try:
            # Detect delimiter by sniffing
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                sample = f.read(4096)
                sniffer = csv.Sniffer()
                try:
                    dialect = sniffer.sniff(sample)
                    delimiter = dialect.delimiter
                except csv.Error:
                    delimiter = "," if file_path.suffix.lower() == ".csv" else "\t"

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f, delimiter=delimiter)
                rows: list[str] = []
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
                    error="File appears to be empty",
                )

            text, was_sampled, description = self._truncate_if_needed(
                full_text,
                f"Header + first {self.MAX_ROWS} rows of {total_rows} total rows",
            )

            return ExtractionResult(
                success=True,
                extraction_method="csv_text",
                text_content=text,
                metadata=metadata,
                was_sampled=was_sampled or total_rows > self.MAX_ROWS + 1,
                sampling_description=description
                if was_sampled
                else (
                    f"First {self.MAX_ROWS} of {total_rows} rows"
                    if total_rows > self.MAX_ROWS + 1
                    else ""
                ),
                content_length=len(text),
            )
        except Exception as e:
            return ExtractionResult(
                success=False,
                extraction_method="csv_text",
                error=str(e),
            )
