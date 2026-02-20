#!/usr/bin/env python3
"""Integration test for Graph API format conversion (Loop/Whiteboard).

Run inside the worker container:
    docker exec sharesentinel-worker python scripts/test_format_conversion.py \
        --loop-url "https://..." --whiteboard-url "https://..."
"""

import argparse
import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

# Add the worker app to the path (container WORKDIR is /app)
sys.path.insert(0, "/app")

from app.config import Config
from app.graph_api.auth import GraphAuth
from app.graph_api.client import GraphClient, GraphAPIError
from app.extraction.text_extractor import TextExtractor
from app.extraction.pdf_extractor import PDFExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def test_format_conversion(
    graph_client: GraphClient,
    url: str,
    label: str,
    output_format: str,
    dest_dir: Path,
) -> bool:
    """Test format conversion for a single sharing URL.

    Returns True on success, False on failure.
    """
    print()
    print("=" * 70)
    print(f"  TEST: {label}")
    print(f"  URL:  {url}")
    print(f"  Format: {output_format}")
    print("=" * 70)

    # ------------------------------------------------------------------
    # Step 1: Resolve sharing URL to a driveItem
    # ------------------------------------------------------------------
    print("\n[1/4] Resolving sharing URL to driveItem ...")
    try:
        metadata = await graph_client.get_item_metadata(object_id=url)
    except GraphAPIError as exc:
        print(f"  FAIL - Graph API error resolving URL: {exc} (status={exc.status_code})")
        return False
    except Exception as exc:
        print(f"  FAIL - Unexpected error resolving URL: {exc}")
        return False

    item_name = metadata.get("name", "<unknown>")
    item_id = metadata.get("id", "")
    parent_ref = metadata.get("parentReference", {})
    drive_id = parent_ref.get("driveId", "")

    print(f"  Item name:  {item_name}")
    print(f"  Item ID:    {item_id}")
    print(f"  Drive ID:   {drive_id}")
    print(f"  Size:       {metadata.get('size', 'N/A')} bytes")
    print(f"  Web URL:    {metadata.get('webUrl', 'N/A')}")

    if not item_id or not drive_id:
        print("  FAIL - Could not extract item_id or drive_id from metadata")
        return False

    print("  OK - driveItem resolved")

    # ------------------------------------------------------------------
    # Step 2: Download with format conversion
    # ------------------------------------------------------------------
    extension = "html" if output_format == "html" else "pdf"
    dest_path = dest_dir / f"test_{label.lower().replace(' ', '_')}.{extension}"

    print(f"\n[2/4] Downloading with format conversion (format={output_format}) ...")
    try:
        await graph_client.download_file_converted(drive_id, item_id, dest_path, output_format)
    except GraphAPIError as exc:
        print(f"  FAIL - Graph API error during conversion: {exc} (status={exc.status_code})")
        return False
    except Exception as exc:
        print(f"  FAIL - Unexpected error during conversion: {exc}")
        return False

    print(f"  Downloaded to: {dest_path}")

    # ------------------------------------------------------------------
    # Step 3: Validate the converted file
    # ------------------------------------------------------------------
    print(f"\n[3/4] Validating converted file ...")

    if not dest_path.exists():
        print("  FAIL - Output file does not exist")
        return False

    file_size = dest_path.stat().st_size
    print(f"  File size: {file_size:,} bytes")

    if file_size == 0:
        print("  FAIL - Output file is empty (0 bytes)")
        return False

    # Content-type validation
    with open(dest_path, "rb") as f:
        header = f.read(256)

    if output_format == "html":
        # HTML should contain at least one angle bracket (tag)
        try:
            text_preview = header.decode("utf-8", errors="replace")
        except Exception:
            text_preview = header.decode("latin-1")
        if "<" not in text_preview:
            print(f"  FAIL - HTML file does not contain '<' in first 256 bytes")
            print(f"  Header preview: {text_preview[:100]!r}")
            return False
        print("  OK - HTML content validated (contains '<' tag)")
    elif output_format == "pdf":
        if not header.startswith(b"%PDF"):
            print(f"  FAIL - PDF file does not start with %PDF magic bytes")
            print(f"  Header bytes: {header[:20]!r}")
            return False
        print("  OK - PDF content validated (starts with %PDF)")

    # ------------------------------------------------------------------
    # Step 4: Run text extraction
    # ------------------------------------------------------------------
    print(f"\n[4/4] Running text extraction ...")

    if output_format == "html":
        extractor = TextExtractor()
        extractor_name = "TextExtractor"
    else:
        extractor = PDFExtractor()
        extractor_name = "PDFExtractor"

    result = extractor.extract(dest_path, file_size)

    print(f"  Extractor:  {extractor_name}")
    print(f"  Success:    {result.success}")
    print(f"  Method:     {result.extraction_method}")
    print(f"  Length:     {result.content_length:,} chars")

    if result.was_sampled:
        print(f"  Sampled:    {result.sampling_description}")

    if result.warnings:
        for w in result.warnings:
            print(f"  Warning:    {w}")

    if result.error:
        print(f"  Error:      {result.error}")

    if result.success and result.text_content:
        preview = result.text_content[:500]
        print(f"\n  --- Extracted text (first 500 chars) ---")
        print(f"  {preview}")
        print(f"  --- End preview ---")
    else:
        print(f"  FAIL - Extraction did not produce text content")
        return False

    print(f"\n  PASS - {label} format conversion and extraction succeeded")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Integration test for Graph API format conversion (Loop/Whiteboard).",
        epilog=(
            "Example:\n"
            '  docker exec sharesentinel-worker python scripts/test_format_conversion.py \\\n'
            '      --loop-url "https://contoso.sharepoint.com/:fl:..." \\\n'
            '      --whiteboard-url "https://contoso.sharepoint.com/:wb:..."'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--loop-url",
        help="Sharing URL for a Loop component to test (converted to HTML)",
    )
    parser.add_argument(
        "--whiteboard-url",
        help="Sharing URL for a Whiteboard to test (converted to PDF)",
    )
    args = parser.parse_args()

    if not args.loop_url and not args.whiteboard_url:
        parser.error("At least one of --loop-url or --whiteboard-url is required")

    # ------------------------------------------------------------------
    # Load config and create Graph client
    # ------------------------------------------------------------------
    print("Loading worker configuration from environment ...")
    config = Config.from_env()

    if not config.azure_tenant_id or not config.azure_client_id:
        print("ERROR: Azure credentials not configured (AZURE_TENANT_ID / AZURE_CLIENT_ID missing)")
        return 1

    auth_method = "certificate" if config.azure_certificate_path else "client_secret"
    print(f"  Tenant ID:    {config.azure_tenant_id}")
    print(f"  Client ID:    {config.azure_client_id}")
    print(f"  Auth method:  {auth_method}")

    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )

    graph_client = GraphClient(auth=auth)

    # ------------------------------------------------------------------
    # Run tests
    # ------------------------------------------------------------------
    tmp_dir = Path(tempfile.mkdtemp(prefix="ss_fmt_test_"))
    print(f"Temp directory: {tmp_dir}")

    results: dict[str, bool] = {}

    try:
        if args.loop_url:
            results["Loop (HTML)"] = await test_format_conversion(
                graph_client=graph_client,
                url=args.loop_url,
                label="Loop (HTML)",
                output_format="html",
                dest_dir=tmp_dir,
            )

        if args.whiteboard_url:
            results["Whiteboard (PDF)"] = await test_format_conversion(
                graph_client=graph_client,
                url=args.whiteboard_url,
                label="Whiteboard (PDF)",
                output_format="pdf",
                dest_dir=tmp_dir,
            )

    finally:
        # Clean up temp directory
        print(f"\nCleaning up temp directory: {tmp_dir}")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)

    all_passed = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed.")
    else:
        print("Some tests FAILED. See details above.")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
