# 10 - Testing and Calibration Plan

## Purpose

This document outlines the testing strategy for ShareSentinel, including unit tests, integration tests, end-to-end tests, and the AI provider benchmarking methodology for comparing Anthropic, OpenAI, and Google Gemini on sensitivity analysis quality and cost.

## Test Categories

### 1. Unit Tests

Each module should have unit tests covering its core logic. Tests should use mocks for external dependencies (Redis, PostgreSQL, Graph API, AI APIs).

**Webhook Listener Tests** (`services/webhook-listener/tests/`):

- `test_validation.py`:
  - Valid payload with all fields → accepts
  - Missing required fields (Operation, UserId, ObjectId, ItemType) → rejects with 400
  - Extra unexpected fields → accepts (Pydantic extra="allow")
  - Malformed JSON → rejects with 400
  - Invalid ObjectId (not a URL) → rejects with 400
  - Various Operation values → all accepted

- `test_deduplication.py`:
  - First event → not a duplicate, returns True for "should process"
  - Same event_id within TTL → duplicate detected
  - Same event_id after TTL expires → not a duplicate (treated as new)
  - Different events with same file but different operation → not duplicates

- `test_webhook.py`:
  - POST to /webhook/splunk with valid payload → 200 with "queued"
  - POST to /webhook/splunk with duplicate → 200 with "duplicate"
  - POST to /webhook/splunk with invalid payload → 400
  - POST with wrong auth secret → 401
  - GET to /health → 200 with health status
  - Redis unavailable → 503

**Worker Tests** (`services/worker/tests/`):

- `test_pipeline/test_classifier.py`:
  - File extensions correctly categorized (excluded, archive, image, text-extractable)
  - Unknown extensions handled gracefully
  - Sensitivity keyword regex matches expected patterns
  - Case-insensitive matching works

- `test_pipeline/test_hasher.py`:
  - SHA-256 computed correctly for known content
  - Hash match found when duplicate content exists in database
  - Hash match not found for new content
  - Hash reuse respects the age threshold (30-day default)

- `test_extraction/test_pdf_extractor.py`:
  - Native PDF → text extracted successfully
  - Scanned PDF (image-only) → extraction returns success=False with appropriate error
  - Empty PDF → extraction returns success=False
  - Multi-page PDF → all pages extracted
  - Large PDF → text truncated to MAX_TEXT_SIZE with was_sampled=True
  - Corrupted PDF → extraction fails gracefully with error message

- `test_extraction/test_docx_extractor.py`:
  - Simple document → body text extracted
  - Document with tables → table content extracted
  - Document with headers/footers → extracted
  - Document properties extracted (title, author, etc.)
  - Empty document → returns success=False
  - Corrupted docx → fails gracefully

- `test_extraction/test_xlsx_extractor.py`:
  - Single-sheet workbook → all rows extracted
  - Multi-sheet workbook → first 10 sheets extracted, remaining sheet names listed
  - Sheet with > 200 rows → sampled to 200 rows
  - Sheet names listed in output
  - Empty workbook → returns success=False
  - Very wide spreadsheet (many columns) → handled within text limit

- `test_extraction/test_pptx_extractor.py`:
  - Slides with text → text extracted
  - Slides with tables → table content extracted
  - Speaker notes → extracted
  - Empty presentation → returns success=False

- `test_extraction/test_csv_extractor.py`:
  - Small CSV → all rows extracted
  - Large CSV (> 500 rows) → sampled to 500 rows, total row count reported
  - TSV file → tab delimiter detected correctly
  - Various encodings (UTF-8, Latin-1) → handled
  - Malformed CSV → handled gracefully

- `test_extraction/test_image_preprocessor.py`:
  - Large image → resized to max 1600px longest edge
  - Small image → unchanged
  - Various formats (PNG, JPEG, TIFF, BMP) → all handled
  - Image with transparency (PNG/RGBA) → preserved as PNG
  - HEIC image → converted successfully
  - Animated GIF → first frame only
  - Output size → under 1MB after compression

- `test_extraction/test_ocr_extractor.py`:
  - Clear scanned document → text extracted via OCR
  - Poor quality scan → OCR attempts, may return insufficient text
  - Multi-page scanned PDF → first 5 pages processed
  - Non-document image → OCR returns minimal text

- `test_extraction/test_archive_extractor.py`:
  - ZIP file → file manifest listed with sizes
  - ZIP with suspicious filenames → manifest includes those names
  - Empty ZIP → handled gracefully
  - Non-ZIP archive types → handled with warning

- `test_ai/test_response_parser.py`:
  - Valid JSON response → parsed correctly
  - JSON with markdown code fences → fences stripped, parsed correctly
  - JSON with extra text before/after → JSON extracted and parsed
  - Completely invalid response → returns default moderate rating with parse_error category
  - Missing fields in JSON → defaults applied
  - Out-of-range sensitivity_rating → clamped to 1-5
  - Invalid confidence value → defaults to "medium"

- `test_ai/test_prompt_manager.py`:
  - Text mode prompt → all template variables filled
  - Multimodal mode prompt → image context included
  - Filename-only mode prompt → reason for content unavailability included
  - Sampled content → sampling notice included in prompt
  - Filename flagged → flag notice included in prompt

- `test_notifications/test_email_notifier.py`:
  - Valid payload → email sent with correct subject and body
  - SMTP failure → returns False, logs error
  - All three alert types generate appropriate emails

### 2. Integration Tests

Integration tests verify that components work together correctly. These require running containers (Redis, PostgreSQL) but can mock external APIs (Graph API, AI APIs).

- **Webhook → Redis Queue**: Send a webhook, verify the job appears in the Redis queue with correct format.
- **Queue → Worker Processing**: Place a job in the Redis queue, verify the worker picks it up and creates a database record.
- **Database operations**: Verify all repository methods work against a real PostgreSQL instance.
- **Full pipeline with mocked externals**: Run the complete pipeline with mocked Graph API (returns test file content) and mocked AI API (returns a predetermined verdict). Verify the correct database records are created and notifications are triggered.
- **Deduplication end-to-end**: Send the same webhook twice, verify only one job is processed.
- **Hash deduplication**: Process a file, then submit a new event for the same content, verify the hash match is detected and the previous verdict is reused.

### 3. End-to-End Tests

End-to-end tests verify the complete system against real external services. These are run manually during deployment and periodically as smoke tests.

- **Test webhook script** (`scripts/test_webhook.sh`): Sends a sample webhook payload to the listener and verifies the response.

```bash
#!/bin/bash
# Send a test webhook to the ShareSentinel listener
WEBHOOK_URL="${WEBHOOK_URL:-http://localhost:8000/webhook/splunk}"
AUTH_SECRET="${WEBHOOK_AUTH_SECRET:-test-secret}"

curl -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AUTH_SECRET" \
  -d '{
    "result": {
      "Operation": "AnonymousLinkCreated",
      "Workload": "OneDrive",
      "UserId": "testuser@organization.com",
      "ObjectId": "https://organization-my.sharepoint.com/personal/testuser_organization_com/Documents/test-file.pdf",
      "SiteUrl": "https://organization-my.sharepoint.com/personal/testuser_organization_com/",
      "SourceFileName": "test-file.pdf",
      "SourceRelativeUrl": "personal/testuser_organization_com/Documents",
      "ItemType": "File",
      "EventSource": "SharePoint",
      "CreationTime": "2024-01-15T10:30:00Z",
      "SharingType": "Anonymous",
      "SharingScope": "Anyone",
      "SharingPermission": "View"
    }
  }'
```

- **Graph API connectivity test**: Verify that the Azure AD app can authenticate and make a basic Graph API call.
- **AI API connectivity test**: Send a simple test prompt to each configured AI provider and verify a response is received.
- **Email delivery test**: Send a test notification email and verify delivery.

### 4. Test Fixtures

Create a set of test files for extraction testing. These should be stored in `services/worker/tests/fixtures/` and be safe (non-sensitive) test content.

**Test files to create:**
- `test.pdf` - Multi-page PDF with text content
- `test_scanned.pdf` - PDF containing images of text (for OCR testing)
- `test.docx` - Word document with body text, tables, headers/footers
- `test.xlsx` - Excel workbook with multiple sheets and various data
- `test.pptx` - PowerPoint with slide text and speaker notes
- `test.csv` - CSV with header and data rows
- `test_large.csv` - CSV with > 500 rows (for sampling testing)
- `test.txt` - Plain text file
- `test.png` - Image file (small)
- `test_large.png` - Image file (> 1600px, for resize testing)
- `test.zip` - ZIP archive with a few named files inside
- `test_empty.pdf` - Empty PDF (for edge case testing)
- `test_empty.docx` - Empty Word document

## AI Provider Benchmarking

### Purpose

Compare Anthropic Claude, OpenAI GPT, and Google Gemini on:
1. **Accuracy**: How well does each provider detect sensitive content?
2. **Consistency**: How much does the rating vary across repeated runs?
3. **Cost**: Token usage and cost per file analyzed.
4. **Speed**: Processing time per analysis.

### Calibration Dataset

Create a dataset of 30-40 test files with known sensitivity levels. These should be synthetic files (not real sensitive documents) that represent realistic content.

**Sensitivity Level 1 (Safe) - 8 files:**
- Public marketing brochure (PDF)
- Team meeting notes with no sensitive topics (DOCX)
- Project timeline spreadsheet with task names only (XLSX)
- Public-facing FAQ document (PDF)
- Conference presentation slides (PPTX)
- CSV of product inventory data (CSV)
- Public event calendar (DOCX)
- Open source project README (TXT)

**Sensitivity Level 2 (Minor) - 6 files:**
- Employee directory with names and work emails only (XLSX)
- Office seating chart with employee names (PDF)
- Department budget summary (no individual salaries) (XLSX)
- Internal process documentation (DOCX)
- Meeting minutes with non-sensitive decisions (DOCX)
- Team contact list with work phone numbers (CSV)

**Sensitivity Level 3 (Moderate) - 6 files:**
- Internal strategic planning document (DOCX)
- Vendor contract with pricing terms (PDF)
- IT system architecture diagram with server names (PNG)
- Quarterly business review slides (PPTX)
- Employee survey results (aggregated, not individual) (XLSX)
- Project proposal with budget details (DOCX)

**Sensitivity Level 4 (High) - 8 files:**
- Spreadsheet with employee names and home addresses (XLSX)
- Student grade report with names and IDs (PDF)
- Performance review document with specific employee feedback (DOCX)
- Salary comparison spreadsheet (XLSX)
- HR complaint documentation (DOCX)
- Legal demand letter (PDF)
- Employee emergency contact list (CSV)
- Board meeting minutes with non-public financial details (DOCX)

**Sensitivity Level 5 (Critical) - 8 files:**
- Fake W-2 tax form with SSN and salary (PDF image)
- Spreadsheet with SSN column (XLSX)
- Scanned driver's license (PNG)
- Medical records document (DOCX)
- Password/credential list (TXT)
- Birth certificate scan (PDF image)
- Employee disciplinary action with personal details (DOCX)
- Financial account statement with account numbers (PDF)

**Important**: All test files must contain SYNTHETIC data only (fake names, fake SSNs, fake addresses). Never use real PII for testing. Generate realistic-looking but clearly fake data.

### Benchmarking Procedure

1. **Setup**: Configure the system to process each test file.

2. **Run each file through all three providers**: For each test file, run the analysis using each of the three AI providers. Record the response.

3. **Run each provider 3 times per file**: To measure consistency, run each file through each provider 3 times.

4. **Record metrics for each run**:
   - Assigned sensitivity_rating
   - Categories detected
   - Summary quality (manual assessment: did it correctly identify the sensitive elements?)
   - Confidence level
   - Input tokens
   - Output tokens
   - Estimated cost
   - Processing time

5. **Score accuracy**: For each run, compare the AI's rating to the "ground truth" rating of the test file. Allow ±1 tolerance (rating of 4 for a ground-truth 5 is acceptable; rating of 2 is not).

6. **Calculate metrics**:
   - **Accuracy**: % of files where AI rating is within ±1 of ground truth
   - **Exact match rate**: % of files where AI rating exactly matches ground truth
   - **Consistency**: Standard deviation of ratings across 3 runs per file per provider
   - **False negative rate**: % of files rated 4-5 (ground truth) that the AI rated ≤ 3 (DANGEROUS; these are missed sensitive files)
   - **False positive rate**: % of files rated 1-2 (ground truth) that the AI rated ≥ 4 (inconvenient but not dangerous)
   - **Average cost per file**: By analysis mode (text vs. multimodal vs. filename-only)
   - **Average processing time**: By provider and analysis mode

7. **Generate comparison report**: A table comparing all three providers across all metrics.

### Priority Weighting

For this use case, the metrics should be weighted as follows:

1. **False negative rate** (MOST IMPORTANT): Missing a truly sensitive file is the worst outcome. The provider with the lowest false negative rate is strongly preferred, even if it has more false positives.
2. **Accuracy** (important): Overall rating accuracy matters, but false negatives matter more than false positives.
3. **Consistency** (important): Inconsistent ratings make the system unreliable. Low variance is desired.
4. **Cost** (moderate importance): Cost matters for long-term operation but should not outweigh accuracy.
5. **Speed** (least important): Given the high latency tolerance, speed is a nice-to-have.

### Benchmarking Script

Create a Python script (`scripts/benchmark_providers.py`) that:
1. Takes a directory of test files and a ground-truth CSV mapping filenames to expected ratings.
2. Runs each file through all three providers (3 runs each).
3. Collects all metrics.
4. Outputs a comparison report as a CSV and a formatted summary.

## Regression Testing

After initial deployment, maintain the calibration dataset as a regression suite. When updating the AI prompt template, changing providers, or modifying the extraction pipeline, re-run the calibration dataset to verify that accuracy has not degraded.

**Automated regression check**: A script that runs the calibration dataset through the current configuration and fails if:
- False negative rate exceeds 5%
- Overall accuracy drops below 80%
- Any ground-truth level 5 file is rated below 3

## Load Testing (Optional)

Given the low volume (< 100 events/day), load testing is not critical for the MVP. However, if desired:

- Use `locust` or a simple script to send 100 webhooks in quick succession.
- Verify all 100 are queued correctly.
- Verify the worker processes all 100 without dropping any.
- Monitor Redis queue depth during the burst.
- Verify the system recovers and returns to an idle state.

## Testing Infrastructure

**Local testing**: Developers can run `docker compose up redis postgres` to start just the dependencies, then run tests against them locally.

**CI/CD**: Tests should be runnable in a CI pipeline. Unit tests need no external dependencies. Integration tests need Redis and PostgreSQL containers (use Docker Compose or test containers). End-to-end tests are manual.

**Test database**: Integration tests should use a separate test database (or create/drop tables for each test run) to avoid contaminating the production database.
