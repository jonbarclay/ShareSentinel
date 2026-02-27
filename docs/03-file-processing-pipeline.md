# 03 - File Processing Pipeline

## Purpose

The file processing pipeline is the core of the worker service. It orchestrates the full lifecycle of processing a sharing event: from receiving a job off the Redis queue through metadata pre-screening, file download, text extraction, AI analysis, verdict recording, analyst notification, and cleanup.

## Pipeline Overview

The pipeline is implemented as a sequence of steps. Each step can succeed, fail with retry, or fail permanently. The orchestrator manages the flow between steps and handles errors at each stage.

```
Job from Redis Queue
        │
        ▼
┌─────────────────────┐
│ 1. Record Event     │  Create initial database record with status "processing"
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ 2. Classify Item    │  Is this a File or Folder?
└────────┬────────────┘
         │
    ┌────┴────┐
    │         │
  Folder    File
    │         │
    ▼         ▼
┌────────┐  ┌─────────────────────┐
│ Flag   │  │ 3. Metadata         │  Get file metadata from Graph API
│ for    │  │    Pre-screen       │  (size, type, name) without downloading
│ Analyst│  └────────┬────────────┘
│ Review │           │
└────────┘           ▼
              ┌─────────────────────┐
              │ 4. Apply Exclusion  │  Is this file type excluded?
              │    Rules            │  (video, audio, binary, etc.)
              └────────┬────────────┘
                       │
                  ┌────┴────┐
                  │         │
              Excluded   Processable
                  │         │
                  ▼         ▼
            ┌──────────┐ ┌─────────────────────┐
            │ Filename │ │ 5. Check File Size   │
            │ /Path    │ └────────┬─────────────┘
            │ Analysis │          │
            │ Only     │     ┌────┴────┐
            └──────────┘     │         │
                          > 50MB    ≤ 50MB
                             │         │
                             ▼         ▼
                       ┌──────────┐ ┌─────────────────────┐
                       │ Filename │ │ 6. Download File     │  Download to tmpfs via Graph API
                       │ /Path    │ └────────┬─────────────┘
                       │ Analysis │          │
                       │ Only     │          ▼
                       └──────────┘ ┌─────────────────────┐
                                    │ 7. Hash + Dedup     │  Compute SHA-256, check DB
                                    └────────┬────────────┘
                                             │
                                        ┌────┴────┐
                                        │         │
                                    Already    New File
                                    Processed     │
                                        │         ▼
                                        ▼   ┌─────────────────────┐
                                  ┌────────┐│ 8. Extract Content   │  Text extraction or
                                  │ Reuse  ││                      │  image preprocessing
                                  │Previous││                      │  (see docs 04 and 05)
                                  │Verdict │└────────┬─────────────┘
                                  └────────┘         │
                                                     ▼
                                              ┌─────────────────────┐
                                              │ 9. AI Analysis       │  Send to configured
                                              │                      │  AI provider
                                              │                      │  (see doc 06)
                                              └────────┬─────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────────┐
                                              │ 10. Record Verdict   │  Store in PostgreSQL
                                              └────────┬─────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────────┐
                                              │ 11. Notify if Risky  │  If Tier 1/2 category,
                                              │                      │  alert analyst
                                              └────────┬─────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────────┐
                                              │ 12. Cleanup          │  Delete temp file,
                                              │                      │  update DB status
                                              └──────────────────────┘
```

## Step Details

### Step 1: Record Event

When a job is pulled from the Redis queue, immediately create a record in the `events` database table with status `processing`. This ensures every event is tracked even if the pipeline crashes midway.

**Fields to record**: event_id, operation, user_id, object_id, file_name, item_type, sharing_type, sharing_permission, event_time, received_at, processing_started_at, status ("processing"), raw_payload.

**Note**: Events now arrive from the audit log poller (in the lifecycle-cron container) rather than a webhook listener. The queue job format is identical regardless of the event source.

### Step 2: Classify Item (File vs. Folder)

Check the `item_type` field from the job payload.

**If "Folder"**: Skip all file processing. Create a verdict record with:
- sensitivity_rating: null (not applicable)
- verdict_type: "folder_share_flagged"
- summary: "Folder shared with [anonymous/org-wide] [view/edit] access. Automatic flag for analyst review."

Trigger analyst notification immediately. Update event status to "completed". Done.

**If "File"**: Continue to Step 3.

**If unrecognized**: Log a warning, treat as "File" and continue. The metadata pre-screen will provide more context.

### Step 3: Metadata Pre-screen

Before downloading the file, make a lightweight Graph API call to get file metadata.

**Graph API call**: Use the `ObjectId` from the job payload to construct a Graph API request for the drive item metadata. The exact API path depends on whether it's a OneDrive or SharePoint file.

For OneDrive personal files:
```
GET https://graph.microsoft.com/v1.0/users/{userId}/drive/root:/{relativePath}/{fileName}
```

For SharePoint files:
```
GET https://graph.microsoft.com/v1.0/sites/{siteId}/drive/root:/{relativePath}/{fileName}
```

The Graph API client module (see `graph_api/client.py`) should handle constructing the correct URL from the job payload fields.

**Metadata to collect**:
- `name`: filename (confirmed from Graph, not just from the log)
- `size`: file size in bytes
- `file.mimeType`: the MIME type
- `webUrl`: the browser-accessible URL
- `parentReference.path`: the folder path
- `createdBy.user.displayName`: who created the file
- `lastModifiedBy.user.displayName`: who last modified
- `shared`: sharing information including the sharing link(s)

**Sharing link retrieval**: To get the actual sharing link that analysts can click, make an additional call:
```
GET https://graph.microsoft.com/v1.0/drives/{driveId}/items/{itemId}/permissions
```
Filter the results for permission entries where the `link` property exists and the `link.scope` is "anonymous" or "organization". Extract the `link.webUrl` as the clickable sharing link.

**Filename sensitivity keywords check**: Run the filename through a regex check against a configurable list of sensitivity keywords. Examples: `ssn`, `w2`, `w-2`, `tax`, `passport`, `salary`, `confidential`, `medical`, `hipaa`, `ferpa`, `grades`, `transcript`, `disciplin`, `social security`, `driver.?license`, `birth.?cert`, `routing.?number`, `account.?number`. If a keyword matches, add a flag to the job context: `filename_flagged = True` with the matched keywords. This information is passed to the AI prompt as additional context.

**Store metadata in the database**: Update the event record with the metadata from Graph API (confirmed file size, mime type, web URL, sharing link URL).

### Step 4: Apply Exclusion Rules

Check the file extension and MIME type against the exclusion list loaded from `config/file_types.yml`.

**Excluded types** (filename/path analysis only):
- Video: .mp4, .mov, .avi, .mkv, .wmv, .flv, .webm, .m4v
- Audio: .mp3, .wav, .m4a, .aac, .flac, .ogg, .wma
- Binary/Executable: .exe, .dll, .bin, .msi, .app, .dmg
- Database: .mdb, .accdb, .sqlite, .db
- Design: .psd, .ai, .sketch, .fig
- 3D/CAD: .dwg, .dxf, .stl, .obj
- Font: .ttf, .otf, .woff
- Any file with no extension (treat as unknown; do filename/path analysis)

**Archive types** (manifest listing + filename analysis):
- .zip, .rar, .7z, .tar, .gz, .tar.gz, .tgz

**Processable types** (full text extraction or multimodal analysis):
- Documents: .pdf, .docx, .doc, .xlsx, .xls, .pptx, .ppt, .odt, .ods, .odp
- Text: .txt, .csv, .tsv, .log, .md, .json, .xml, .html, .htm, .rtf
- Images: .png, .jpg, .jpeg, .tiff, .tif, .bmp, .gif, .webp, .heic
- Email: .msg, .eml

**Config file format** (`config/file_types.yml`):
```yaml
excluded_extensions:
  - .mp4
  - .mov
  # ... etc

archive_extensions:
  - .zip
  - .rar
  # ... etc

image_extensions:
  - .png
  - .jpg
  - .jpeg
  - .tiff
  - .tif
  - .bmp
  - .gif
  - .webp
  - .heic

text_extractable_extensions:
  pdf: "pdf_extractor"
  docx: "docx_extractor"
  doc: "docx_extractor"  # python-docx can handle .doc in many cases
  xlsx: "xlsx_extractor"
  xls: "xlsx_extractor"
  pptx: "pptx_extractor"
  ppt: "pptx_extractor"
  csv: "csv_extractor"
  tsv: "csv_extractor"
  txt: "text_extractor"
  log: "text_extractor"
  md: "text_extractor"
  json: "text_extractor"
  xml: "text_extractor"
  html: "text_extractor"
  htm: "text_extractor"
  rtf: "text_extractor"

sensitivity_keywords:
  - "ssn"
  - "w-?2"
  - "tax"
  - "passport"
  - "salary"
  - "confidential"
  - "medical"
  - "hipaa"
  - "ferpa"
  - "grades"
  - "transcript"
  - "disciplin"
  - "social.?security"
  - "driver.?licen"
  - "birth.?cert"
  - "routing.?number"
  - "account.?number"
  - "secret"
  - "password"
  - "credential"
```

If the file extension is in `excluded_extensions`, route to filename/path-only AI analysis.

If the file extension is in `archive_extensions`, route to archive manifest extraction (see doc 04).

Otherwise, proceed to Step 5.

### Step 5: Check File Size

Compare the file size (from the Graph API metadata in Step 3) against the download threshold.

- **Over 50MB**: Do not download. Route to filename/path-only AI analysis. Record the reason: "file_too_large".
- **50MB or under**: Proceed to download.

### Step 6: Download File

Download the file from SharePoint/OneDrive to the tmpfs mount using the Graph API.

**Graph API download**: 
```
GET https://graph.microsoft.com/v1.0/drives/{driveId}/items/{itemId}/content
```

This returns the file content as a binary stream. Stream the response directly to a file on the tmpfs mount. Do not buffer the entire file in memory.

**File path on tmpfs**: `/tmp/sharesentinel/{event_id}/{original_filename}`

Using the event_id as a subdirectory prevents filename collisions between concurrent jobs (if we ever scale to multiple worker instances).

**Error handling**: 
- If the Graph API returns 404, the file has been deleted or the sharing link has been removed since the alert fired. Record as "file_not_found" and complete processing (no further analysis needed since the share no longer exists).
- If the Graph API returns 403, the app doesn't have access. Record as "access_denied" and flag for investigation (possible permissions issue with the Azure AD app).
- If the download times out or fails, retry up to 3 times with exponential backoff.

### Step 7: Hash and Dedup Check

After downloading the file, compute a SHA-256 hash of the file content.

Check the `file_hashes` database table for this hash. If found:
- The same file content was previously analyzed.
- If the previous verdict was recorded within the last 30 days, reuse it. Update the current event record with the previous verdict, a note that it was a "hash_match_reuse", and the original event_id that produced the verdict.
- Skip AI analysis. Proceed to Step 10 (record verdict) with the reused verdict.
- Still trigger notification if the reused verdict had Tier 1/2 categories (the file is being shared again and is still risky).

If not found, this is new content. Store the hash in the `file_hashes` table linked to this event_id.

### Step 8: Extract Content

This step routes to the appropriate extraction strategy based on file type. See **doc 04 (Text Extraction Module)** and **doc 05 (Image Preprocessing Module)** for full details.

**Decision tree**:

1. Is the file extension in `image_extensions`? → Route to image preprocessing (doc 05). Result: compressed image for multimodal analysis.

2. Is the file extension in `text_extractable_extensions`? → Route to the corresponding text extractor (doc 04). 
   - If text extraction succeeds and produces > 50 characters of meaningful content → Result: extracted text for text-based AI analysis.
   - If text extraction fails or produces < 50 characters → Is this a PDF? 
     - Yes → Try OCR (Tesseract). If OCR succeeds → Result: OCR text for text-based analysis.
     - If OCR fails or produces < 50 characters → Extract first 3 pages as images → Result: images for multimodal analysis.
     - No → Route to filename/path-only analysis.

3. Is the file extension in `archive_extensions`? → List internal filenames without extracting (doc 04). Result: file manifest text for text-based AI analysis.

4. Unrecognized file type → Route to filename/path-only analysis.

**Content size check after extraction**: If extracted text exceeds 100KB (~25,000 tokens), apply the sampling strategy for that file type (see doc 04 for per-type sampling rules). The goal is to keep the text under 100KB before sending to the AI.

**Extraction context object**: The extraction step produces a context object that is passed to the AI analysis step:

```python
@dataclass
class ExtractionResult:
    extraction_method: str         # "text_extraction", "ocr", "multimodal", "archive_manifest", "filename_only"
    text_content: Optional[str]    # Extracted text (for text-based analysis)
    images: Optional[List[bytes]]  # Compressed images (for multimodal analysis)
    image_count: Optional[int]     # Number of images being sent
    original_file_size: int        # Size of the original file in bytes
    extracted_content_size: int    # Size of the extracted text or images in bytes
    was_sampled: bool              # Whether the content was truncated/sampled
    sampling_description: str      # Human-readable description of what was sampled
    file_metadata: dict            # Document properties, sheet names, page count, etc.
    extraction_warnings: List[str] # Any issues encountered during extraction
```

### Step 9: AI Analysis

Send the extracted content to the configured AI provider. See **doc 06 (AI Provider Abstraction)** for full details on provider interface, prompt construction, and response parsing.

**Three analysis modes based on extraction result**:

1. **Text-based analysis**: Send extracted text as a text prompt. Cheapest and most common path.
2. **Multimodal analysis**: Send images along with a text prompt. Used for actual images and scanned documents.
3. **Filename/path-only analysis**: Send only the filename, path, file size, and sharing metadata. Used for excluded types, oversized files, and files where all extraction methods failed.

**AI response**: The AI returns a structured JSON response with category-based sensitivity detection:

```json
{
  "categories": ["pii_government_id", "pii_financial"],
  "context": "mixed",
  "summary": "This file appears to be a W-2 tax form containing an employee's SSN, employer identification number, and salary information.",
  "recommendation": "This file should not be shared with anonymous or organization-wide access. Contact the user to restrict sharing."
}
```

**Sensitivity categories** are organized into tiers for escalation:
- **Tier 1 (urgent)**: `pii_government_id`, `pii_financial`, `ferpa`, `hipaa`, `security_credentials`
- **Tier 2 (normal)**: `hr_personnel`, `legal_confidential`, `pii_contact`
- **Tier 3 (no escalation)**: `coursework`, `casual_personal`, `none`

Escalation is deterministic: any Tier 1 or Tier 2 category triggers analyst notification. There is no configurable threshold.

See doc 06 for the full AI prompt template, structured output enforcement, and response parsing.

### Step 10: Record Verdict

Store the AI verdict in the `verdicts` database table:
- event_id (FK to events table)
- categories_detected (JSON array of category strings)
- context (text — "mixed", "educational", "personal", etc.)
- summary (text)
- recommendation (text)
- ai_provider (which provider was used)
- ai_model (which specific model)
- input_tokens (for cost tracking)
- output_tokens (for cost tracking)
- estimated_cost (calculated from token counts and provider pricing)
- analysis_mode ("text", "multimodal", "filename_only")
- processing_duration_seconds
- verdict_at (timestamp)

Update the event record status from "processing" to "completed".

Store the file hash in `file_hashes` if not already present (from Step 7).

**Note**: The `sensitivity_rating` column is retained for backward compatibility with older rows but is nullable and no longer populated by the current category-based rubric.

### Step 10.5: Enroll Sharing Links in Lifecycle

After recording the verdict, the pipeline enrolls each anonymous/org-wide sharing permission into the `sharing_link_lifecycle` table. This enables the 180-day countdown process:

- Fetch sharing permissions from the Graph API (`GET /drives/{driveId}/items/{itemId}/permissions`)
- For each permission with `link.scope` of "anonymous" or "organization", insert a lifecycle row
- Links with a Microsoft-set `expirationDateTime` are marked `ms_managed` and exempt from countdown notifications
- Links without Microsoft expiration are marked `active` and start the 180-day countdown from `event_time`

See **doc 11 (Sharing Link Lifecycle)** for the full lifecycle process.

### Step 11: Notify if Risky

If any detected category is Tier 1 or Tier 2, trigger analyst notification. See **doc 08 (Notification Service)** for details.

**Notification payload**:
- File name
- File path
- Who shared it (user email/UPN)
- When the sharing link was created
- Sharing type (anonymous or org-wide)
- Sharing permission (view or edit)
- Categories detected
- AI summary
- Clickable sharing link (so the analyst can view the file directly)
- Link to the event record in the database (for audit purposes)

Also notify for folder shares (from Step 2) with appropriate folder-specific messaging.

### Step 12: Cleanup

**Delete the temporary file** from tmpfs. Delete the entire event subdirectory (`/tmp/sharesentinel/{event_id}/`).

**Verify deletion**: After deletion, confirm the file no longer exists. Log a warning if deletion fails (the safety net cleanup task will catch it, but this shouldn't happen).

**Update event record**: Set `processing_completed_at` timestamp. Set `temp_file_deleted` to true.

## Worker Main Loop

The worker runs a continuous loop:

```python
async def main():
    # Initialize connections
    redis = await connect_redis()
    db = await connect_postgres()
    
    # Start background cleanup task
    asyncio.create_task(cleanup_stale_files())
    
    # Main processing loop
    while True:
        try:
            # Blocking pop from Redis queue (waits for a job)
            _, job_json = await redis.blpop("sharesentinel:jobs")
            job = QueueJob.parse_raw(job_json)
            
            # Process the job through the pipeline
            await process_job(job, db, redis)
            
        except Exception as e:
            logger.error(f"Unhandled exception in main loop: {e}")
            # Sleep briefly to avoid tight loop on persistent errors
            await asyncio.sleep(5)
```

## Background Cleanup Task

A background task runs every 5 minutes and scans the tmpfs mount for any files older than 30 minutes. This catches files that were missed by the normal cleanup step (e.g., if the worker crashed mid-processing).

```python
async def cleanup_stale_files():
    while True:
        await asyncio.sleep(300)  # 5 minutes
        tmpfs_path = Path("/tmp/sharesentinel")
        cutoff = time.time() - 1800  # 30 minutes ago
        for event_dir in tmpfs_path.iterdir():
            if event_dir.is_dir() and event_dir.stat().st_mtime < cutoff:
                shutil.rmtree(event_dir)
                logger.warning(f"Cleaned up stale directory: {event_dir.name}")
```

## Retry Logic

External calls (Graph API, AI APIs, database writes, email sends) use a common retry wrapper:

```python
async def retry_with_backoff(func, max_retries=3, base_delay=2):
    for attempt in range(max_retries):
        try:
            return await func()
        except RetryableError as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)  # 2s, 4s, 8s
            logger.warning(f"Retry {attempt + 1}/{max_retries} after {delay}s: {e}")
            await asyncio.sleep(delay)
```

**Retryable errors**: Network timeouts, HTTP 429 (rate limit), HTTP 5xx (server error), connection refused.

**Non-retryable errors**: HTTP 400 (bad request), HTTP 401 (auth failure), HTTP 403 (forbidden), HTTP 404 (not found), file parsing errors, validation errors.

## Concurrency

The worker processes up to `MAX_CONCURRENT_JOBS` (default: 5) jobs concurrently using `asyncio.Semaphore`. This is sufficient for < 100 events/day with burst capacity during backfills.

For future scaling:
- Multiple worker instances can consume from the same Redis list (BLPOP is safe for concurrent consumers; each job is delivered to exactly one consumer).
- The tmpfs mount and event_id subdirectory structure prevent file collisions.
- Database writes use the event_id as the primary key, so concurrent workers won't conflict.
- The file hash dedup check should use a database lock or "SELECT FOR UPDATE" to prevent race conditions where two workers download and process the same file simultaneously.
