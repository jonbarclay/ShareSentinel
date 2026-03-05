# 07 - Database Schema and Audit Logging

## Purpose

PostgreSQL stores all persistent state for ShareSentinel: event records, AI verdicts, file hashes for deduplication, audit logs, and operational metadata. The schema is designed to support the processing pipeline, analyst workflows, cost tracking, and troubleshooting.

## Database: `sharesentinel`

## Tables

### 1. events

The primary table. One row per sharing event received from the audit log poller.

```sql
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) UNIQUE NOT NULL,        -- SHA-256 hash used for deduplication
    
    -- From audit log poller
    operation VARCHAR(100) NOT NULL,              -- e.g., "AnonymousLinkCreated"
    workload VARCHAR(50),                         -- "OneDrive" or "SharePoint"
    user_id VARCHAR(255) NOT NULL,                -- UPN of sharing user
    object_id TEXT NOT NULL,                      -- Full URL to the shared item
    site_url TEXT,                                -- SharePoint site or OneDrive URL
    file_name VARCHAR(500),                       -- Original filename
    relative_path TEXT,                           -- Relative path within the site
    item_type VARCHAR(20) NOT NULL,               -- "File" or "Folder"
    sharing_type VARCHAR(50),                     -- "Anonymous" or "Company"
    sharing_scope VARCHAR(50),                    -- Scope of sharing
    sharing_permission VARCHAR(20),               -- "View" or "Edit"
    event_time TIMESTAMP WITH TIME ZONE,          -- When the sharing event occurred
    
    -- Graph API metadata (populated during processing)
    confirmed_file_name VARCHAR(500),             -- Filename confirmed from Graph API
    file_size_bytes BIGINT,                       -- File size from Graph API
    mime_type VARCHAR(100),                       -- MIME type from Graph API
    web_url TEXT,                                 -- Browser-accessible URL
    sharing_link_url TEXT,                        -- The actual sharing link for analysts
    drive_id VARCHAR(255),                        -- Graph API drive ID
    item_id_graph VARCHAR(255),                   -- Graph API item ID
    
    -- Processing state
    status VARCHAR(30) NOT NULL DEFAULT 'queued', -- queued, processing, completed, failed
    processing_started_at TIMESTAMP WITH TIME ZONE,
    processing_completed_at TIMESTAMP WITH TIME ZONE,
    
    -- Processing details
    file_category VARCHAR(30),                    -- "processable", "excluded", "archive", "image", "oversized", "folder"
    extraction_method VARCHAR(50),                -- "pdf_text", "docx_text", "ocr", "multimodal", "filename_only", etc.
    was_sampled BOOLEAN DEFAULT FALSE,
    sampling_description TEXT,
    file_hash VARCHAR(64),                        -- SHA-256 of file content (NULL for folders or undownloaded files)
    hash_match_reuse BOOLEAN DEFAULT FALSE,       -- Whether verdict was reused from a previous hash match
    hash_match_event_id VARCHAR(64),              -- Event ID of the original analysis if reused
    filename_flagged BOOLEAN DEFAULT FALSE,       -- Whether filename matched sensitivity keywords
    filename_flag_keywords TEXT,                   -- Comma-separated matched keywords
    
    -- Failure info
    failure_reason TEXT,                           -- Description of failure if status = 'failed'
    retry_count INT DEFAULT 0,                    -- Number of retries attempted
    
    -- Cleanup tracking
    temp_file_deleted BOOLEAN DEFAULT FALSE,
    
    -- Metadata
    received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    raw_payload JSONB                             -- Complete original audit log record
);

-- Indexes
CREATE INDEX idx_events_status ON events(status);
CREATE INDEX idx_events_event_time ON events(event_time);
CREATE INDEX idx_events_user_id ON events(user_id);
CREATE INDEX idx_events_file_hash ON events(file_hash);
CREATE INDEX idx_events_received_at ON events(received_at);
CREATE INDEX idx_events_sensitivity ON events(status) WHERE status = 'completed';
```

### 2. verdicts

AI analysis results. One row per completed analysis. Linked to the events table.

```sql
CREATE TABLE verdicts (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    
    -- AI verdict (category-based sensitivity detection)
    sensitivity_rating INT,                              -- DEPRECATED: nullable, kept for backward compat with old rows
    categories_detected JSONB DEFAULT '[]'::jsonb,       -- JSON array of category strings (e.g., ["pii_financial", "ferpa"])
    context VARCHAR(50),                                 -- "mixed", "educational", "personal", etc.
    summary TEXT,                                         -- AI's summary of findings
    recommendation TEXT,                                  -- AI's recommended action
    
    -- Analysis metadata
    analysis_mode VARCHAR(20) NOT NULL,                -- "text", "multimodal", "filename_only"
    ai_provider VARCHAR(20) NOT NULL,                  -- "anthropic", "openai", "gemini"
    ai_model VARCHAR(100) NOT NULL,                    -- Specific model used
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    estimated_cost_usd DECIMAL(10, 6) DEFAULT 0,
    processing_time_seconds DECIMAL(8, 2),
    
    -- Notification tracking
    notification_required BOOLEAN DEFAULT FALSE,       -- Whether any Tier 1/2 category was detected
    notification_sent BOOLEAN DEFAULT FALSE,
    notification_sent_at TIMESTAMP WITH TIME ZONE,
    notification_channel VARCHAR(20),                  -- "email", "jira"
    notification_reference VARCHAR(255),               -- Email message ID or Jira ticket key
    
    -- Analyst disposition (updated by analysts or future dashboard)
    analyst_reviewed BOOLEAN DEFAULT FALSE,
    analyst_reviewed_at TIMESTAMP WITH TIME ZONE,
    analyst_reviewed_by VARCHAR(255),
    analyst_disposition VARCHAR(50),                   -- "confirmed_sensitive", "false_positive", "action_taken", "no_action_needed"
    analyst_notes TEXT,
    
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_verdicts_event_id ON verdicts(event_id);
-- idx_verdicts_rating removed (sensitivity_rating is deprecated)
CREATE INDEX idx_verdicts_notification ON verdicts(notification_required, notification_sent);
CREATE INDEX idx_verdicts_provider ON verdicts(ai_provider);
CREATE INDEX idx_verdicts_created ON verdicts(created_at);
```

### 3. file_hashes

Tracks file content hashes for deduplication across events. If the same file content is shared via a different link or by a different user, we can reuse the previous analysis.

```sql
CREATE TABLE file_hashes (
    id SERIAL PRIMARY KEY,
    file_hash VARCHAR(64) UNIQUE NOT NULL,             -- SHA-256 of file content
    first_event_id VARCHAR(64) NOT NULL,               -- Event ID of the first analysis
    sensitivity_rating INT,                             -- Rating from the first analysis
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    times_seen INT DEFAULT 1,                          -- Number of times this hash has appeared
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_file_hashes_hash ON file_hashes(file_hash);
CREATE INDEX idx_file_hashes_last_seen ON file_hashes(last_seen_at);
```

### 4. audit_log

Operational audit log for troubleshooting. Records key actions and state transitions. This is separate from application log files and provides a queryable audit trail.

```sql
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64),                              -- Related event (NULL for system-level entries)
    action VARCHAR(100) NOT NULL,                      -- e.g., "event_received", "file_downloaded", "ai_analysis_completed"
    details JSONB,                                     -- Action-specific details
    status VARCHAR(20),                                -- "success", "failure", "warning"
    error_message TEXT,                                -- Error details if status = 'failure'
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_log_event_id ON audit_log(event_id);
CREATE INDEX idx_audit_log_action ON audit_log(action);
CREATE INDEX idx_audit_log_created ON audit_log(created_at);
CREATE INDEX idx_audit_log_status ON audit_log(status);
```

**Audit log entries to record:**

| Action | When | Details |
|--------|------|---------|
| `audit_event_ingested` | Audit log poller enqueues a new event | operation, user_id, file_name, item_type |
| `duplicate_detected` | Deduplication catches a repeat event | event_id, original_received_at |
| `lifecycle_enrolled` | Sharing link enrolled in 180-day lifecycle | permission_id, status (active/ms_managed) |
| `lifecycle_notified` | Countdown notification sent to file owner | milestone, days_remaining |
| `lifecycle_removed` | Sharing link removed at 180-day mark | permission_id, success |
| `processing_started` | Worker picks up the job | event_id |
| `metadata_fetched` | Graph API metadata call succeeds | file_size, mime_type |
| `file_excluded` | File type is in exclusion list | extension, reason |
| `file_too_large` | File exceeds download threshold | file_size, threshold |
| `file_downloaded` | File successfully downloaded to tmpfs | file_size, download_time_seconds |
| `file_not_found` | Graph API returns 404 | object_id |
| `hash_computed` | File hash computed | hash (first 8 chars only for privacy) |
| `hash_match_found` | File content matches a previous analysis | original_event_id, original_rating |
| `text_extraction_started` | Text extraction begins | extraction_method |
| `text_extraction_completed` | Text extraction succeeds | content_length, was_sampled |
| `text_extraction_failed` | Text extraction fails | error_message, fallback_method |
| `ocr_started` | OCR processing begins | page_count |
| `ocr_completed` | OCR succeeds | content_length |
| `ocr_failed` | OCR fails | error_message |
| `image_preprocessed` | Image resized/compressed | original_size, processed_size |
| `ai_analysis_started` | AI API call begins | provider, model, mode |
| `ai_analysis_completed` | AI API call succeeds | rating, input_tokens, output_tokens, cost |
| `ai_analysis_failed` | AI API call fails | error_message, retry_count |
| `ai_parse_error` | AI response couldn't be parsed as JSON | raw_response_preview |
| `notification_sent` | Analyst notification sent | channel, recipient, rating |
| `notification_failed` | Notification delivery failed | channel, error_message |
| `temp_file_deleted` | Temp file cleaned up | file_path |
| `stale_file_cleaned` | Background cleanup found a stale file | file_path, age_minutes |
| `processing_completed` | Full pipeline finished | total_duration_seconds, verdict |
| `processing_failed` | Pipeline failed permanently | failure_reason |
| `folder_share_flagged` | Folder share detected and flagged | user_id, object_id |

### 5. sharing_link_lifecycle

Tracks the 180-day lifecycle of anonymous and org-wide sharing links, from creation through notification milestones to automatic removal.

```sql
CREATE TABLE IF NOT EXISTS sharing_link_lifecycle (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events(event_id),
    permission_id VARCHAR(255) NOT NULL,
    drive_id VARCHAR(255) NOT NULL,
    item_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL,

    link_created_at TIMESTAMPTZ NOT NULL,        -- Day zero (from event_time)
    ms_expiration_at TIMESTAMPTZ,                -- From Graph expirationDateTime (NULL if none)

    -- 'active', 'ms_managed', 'expired_removed', 'manually_removed', 'error'
    status VARCHAR(30) NOT NULL DEFAULT 'active',

    -- Notification milestones (NULL = not yet sent)
    notified_120d_at TIMESTAMPTZ,
    notified_150d_at TIMESTAMPTZ,
    notified_165d_at TIMESTAMPTZ,
    notified_173d_at TIMESTAMPTZ,
    notified_178d_at TIMESTAMPTZ,
    notified_180d_at TIMESTAMPTZ,

    -- Removal tracking
    removal_attempted_at TIMESTAMPTZ,
    removal_succeeded BOOLEAN,
    removal_error TEXT,

    -- Context for notifications (avoid joins)
    file_name VARCHAR(500),
    sharing_scope VARCHAR(50),
    sharing_type VARCHAR(50),
    link_url TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (event_id, permission_id)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_status ON sharing_link_lifecycle(status);
CREATE INDEX IF NOT EXISTS idx_lifecycle_active_due ON sharing_link_lifecycle(link_created_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_lifecycle_user_id ON sharing_link_lifecycle(user_id);
```

**Status values:**
- `active` — Link is in the 180-day countdown. Will receive milestone notifications.
- `ms_managed` — Microsoft set an `expirationDateTime` on this link. Exempt from our countdown and removal.
- `expired_removed` — Link was automatically removed at the 180-day mark.
- `manually_removed` — Analyst or user removed the link before expiration.
- `error` — Removal was attempted but failed (see `removal_error`).

### 6. audit_poll_state

Single-row table tracking the audit log poller's progress.

```sql
CREATE TABLE IF NOT EXISTS audit_poll_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_poll_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_poll_status TEXT DEFAULT 'success',
    events_found INTEGER DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);
```

### 7. configuration (optional, for future use)

Stores configurable parameters that might be adjusted without redeployment. For the MVP, configuration comes from environment variables and config files. This table is a placeholder for a future admin UI.

```sql
CREATE TABLE configuration (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(255)
);

-- Seed with defaults
INSERT INTO configuration (key, value, description) VALUES
    ('max_file_size_bytes', '52428800', 'Maximum file size to download (50MB)'),
    ('text_content_limit', '100000', 'Maximum extracted text size in characters'),
    ('hash_reuse_days', '30', 'Days to consider a previous hash analysis valid for reuse'),
    ('dedup_ttl_seconds', '86400', 'Deduplication cache TTL in seconds');

-- Note: sensitivity_threshold has been removed. Escalation is now deterministic
-- based on category tiers (Tier 1/2 = escalate). No configurable threshold.
```

## Migration Strategy

Use numbered SQL migration files in `services/worker/app/database/migrations/`:

```
migrations/
├── 001_initial.sql        # Creates all tables, indexes, and seed data
├── 002_add_field.sql      # Example future migration
└── ...
```

The worker should check on startup whether migrations have been applied and run any pending ones. A simple approach: create a `schema_migrations` table that tracks which migration files have been applied.

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INT PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
```

## Data Access Layer

The repository pattern provides clean data access methods:

```python
class EventRepository:
    async def create_event(self, job: QueueJob) -> int
    async def update_event_status(self, event_id: str, status: str, **kwargs)
    async def update_event_metadata(self, event_id: str, metadata: dict)
    async def get_event(self, event_id: str) -> Optional[dict]
    async def get_events_by_status(self, status: str, limit: int = 50) -> List[dict]
    async def get_events_by_user(self, user_id: str, limit: int = 50) -> List[dict]

class VerdictRepository:
    async def create_verdict(self, event_id: str, response: AnalysisResponse) -> int
    async def update_notification_status(self, event_id: str, sent: bool, channel: str, reference: str)
    async def update_analyst_disposition(self, event_id: str, disposition: str, reviewed_by: str, notes: str)
    async def get_verdict(self, event_id: str) -> Optional[dict]
    async def get_pending_notifications(self) -> List[dict]
    async def get_verdicts_by_rating(self, min_rating: int, limit: int = 50) -> List[dict]

class FileHashRepository:
    async def check_hash(self, file_hash: str, max_age_days: int = 30) -> Optional[dict]
    async def store_hash(self, file_hash: str, event_id: str, sensitivity_rating: int)
    async def update_last_seen(self, file_hash: str)

class AuditLogRepository:
    async def log(self, event_id: Optional[str], action: str, details: dict = None, status: str = "success", error: str = None)
    async def get_logs_for_event(self, event_id: str) -> List[dict]
    async def get_recent_failures(self, hours: int = 24) -> List[dict]
```

## Connection Management

Use connection pooling via `asyncpg` (async PostgreSQL driver):

```python
import asyncpg

async def create_pool():
    return await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
```

## Data Retention

For the MVP, keep all data indefinitely (volume is low, < 100 records/day). For long-term production use, consider:

- `audit_log`: Retain 90 days, then archive or delete.
- `events` and `verdicts`: Retain 1 year.
- `file_hashes`: Retain indefinitely (they're small and useful for long-term dedup).

Implement retention as a periodic cleanup job (weekly cron or background task).

## Security Notes

- The `raw_payload` column in `events` stores the complete audit log record as JSONB. This is useful for debugging but may contain sensitive metadata. Access to the database should be restricted.
- The `summary` field in `verdicts` may contain AI-generated descriptions of sensitive content (e.g., "This file contains SSNs for 500 employees"). This field should be treated as sensitive and not exposed in application logs.
- Database credentials should be managed via Docker secrets or environment variables, never hardcoded.
- The PostgreSQL instance should only be accessible from the Docker Compose internal network, not exposed on any host port.
