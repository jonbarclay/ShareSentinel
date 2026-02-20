# 01 - Architecture Overview

## System Purpose

ShareSentinel monitors file sharing activity in OneDrive and SharePoint. When a user creates an anonymous link or an organization-wide sharing link, the system automatically evaluates whether the shared file contains sensitive content. If the content appears risky, human analysts are notified so they can reach out to the user.

## Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL SYSTEMS                              │
│                                                                         │
│  SharePoint/OneDrive ──► Azure Audit Logs ──► Splunk ──► Webhook        │
│                                                                         │
│  Microsoft Graph API (file metadata + download)                         │
│  AI APIs (Anthropic / OpenAI / Gemini)                                  │
│  SMTP Server (analyst email notifications)                              │
│  Jira API (Phase 2 - ticket creation)                                   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         SHARESENTINEL SYSTEM                            │
│                                                                         │
│  ┌──────────────────┐    ┌───────┐    ┌──────────────────────────────┐  │
│  │ Webhook Listener │───►│ Redis │───►│ Worker                       │  │
│  │ (FastAPI)        │    │       │    │                              │  │
│  │                  │    │ Queue │    │ ┌──────────────────────────┐ │  │
│  │ - Validate       │    │ Dedup │    │ │ Pipeline Orchestrator    │ │  │
│  │ - Deduplicate    │    │ Cache │    │ │                          │ │  │
│  │ - Enqueue        │    │       │    │ │ 1. Metadata Pre-screen   │ │  │
│  │                  │    └───────┘    │ │ 2. Download to tmpfs     │ │  │
│  └──────────────────┘                 │ │ 3. Hash + Dedup Check    │ │  │
│                                       │ │ 4. Text Extraction       │ │  │
│                                       │ │ 5. OCR Fallback          │ │  │
│                                       │ │ 6. Image Preprocessing   │ │  │
│                                       │ │ 7. AI Analysis           │ │  │
│                                       │ │ 8. Verdict Recording     │ │  │
│                                       │ │ 9. Notification          │ │  │
│                                       │ │ 10. Cleanup              │ │  │
│                                       │ └──────────────────────────┘ │  │
│                                       └──────────────┬───────────────┘  │
│                                                      │                  │
│                                              ┌───────▼───────┐         │
│                                              │  PostgreSQL    │         │
│                                              │               │         │
│                                              │ - Events      │         │
│                                              │ - Verdicts    │         │
│                                              │ - File Hashes │         │
│                                              │ - Audit Log   │         │
│                                              │ - Config      │         │
│                                              └───────────────┘         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Container Layout

### Container 1: webhook-listener

**Purpose**: Receives HTTP POST requests from Splunk, validates the payload, checks for duplicates, and pushes the job onto the Redis queue.

**Technology**: Python 3.12, FastAPI, uvicorn

**Ports**: Exposes port 8000 (configurable) for incoming webhooks.

**Connections**: Redis (for deduplication and queue push).

**Stateless**: Yes. No local state. Can be restarted without data loss.

**Resource requirements**: Minimal. Handles < 100 requests/day.

### Container 2: worker

**Purpose**: Consumes jobs from the Redis queue and executes the full file processing pipeline.

**Technology**: Python 3.12, with libraries for text extraction (PyMuPDF, python-docx, openpyxl, python-pptx), OCR (Tesseract via pytesseract), image processing (Pillow), and AI API clients.

**Mounts**: tmpfs mount at `/tmp/sharesentinel` for temporary file storage (RAM-backed, never persists to disk).

**Connections**: Redis (queue consumption), PostgreSQL (verdict storage, audit logging), Microsoft Graph API (file download), AI APIs (analysis), SMTP (notifications).

**Stateless**: Yes, aside from temporary files in tmpfs which are cleaned up after each job.

**Resource requirements**: Moderate. Needs enough RAM for tmpfs file storage (allocate 256MB for tmpfs, sufficient for the largest files we'll download). CPU usage spikes during text extraction and OCR.

**System dependencies**: Tesseract OCR must be installed in the Docker image (`apt-get install tesseract-ocr`).

### Container 3: redis

**Purpose**: Job queue (Redis list or stream), deduplication cache (Redis set with TTL), and general caching.

**Technology**: Redis 7+ (official Docker image).

**Persistence**: Optional. Since jobs are low-volume and latency is not critical, Redis persistence (RDB snapshots) is nice-to-have for surviving container restarts, but not strictly required. If Redis loses its state, unprocessed webhooks would need to be re-sent by Splunk (which Splunk can retry).

**Resource requirements**: Minimal. The queue will never hold more than a few hundred items.

### Container 4: postgres

**Purpose**: Persistent storage for event records, AI verdicts, file hashes, audit logs, and configuration.

**Technology**: PostgreSQL 16 (official Docker image).

**Persistence**: Required. Uses a Docker volume for data persistence.

**Resource requirements**: Minimal. Database will be small (< 100 records/day, each a few KB).

## Technology Rationale

**FastAPI over Flask**: FastAPI provides automatic request validation via Pydantic, async support (useful if we ever need concurrent webhook processing), and built-in OpenAPI documentation. For this project's volume, either would work, but FastAPI's Pydantic integration makes payload validation cleaner.

**Redis over RabbitMQ**: Redis is simpler to operate, has a smaller footprint, and provides everything we need (basic queue, deduplication cache, TTL-based key expiry). RabbitMQ would be overkill for < 100 jobs/day.

**PostgreSQL over SQLite**: PostgreSQL is the better choice for a multi-container setup (the worker needs database access, and if we ever add a dashboard container, it also needs access). SQLite doesn't handle concurrent access from multiple processes well. PostgreSQL also has a cleaner migration path to Kubernetes (managed database services).

**tmpfs over disk-based temp storage**: Files potentially contain the most sensitive content in the organization. tmpfs ensures they exist only in RAM, are never written to persistent disk, and are automatically purged on container restart. This is a significant security benefit.

**Tesseract OCR over cloud OCR services**: Keeps OCR processing local (no additional data leaving the system), is free, and is sufficient quality for cleanly scanned documents. Cloud OCR (Azure Document Intelligence, Google Document AI) would be higher quality but adds another external data dependency and cost.

## Communication Patterns

All inter-container communication uses the Docker Compose internal network.

- **Webhook Listener → Redis**: Direct Redis client connection (redis-py library). Pushes JSON job payloads onto a Redis list.
- **Worker → Redis**: Direct Redis client connection. Blocking pop (BLPOP) from the Redis list to consume jobs.
- **Worker → PostgreSQL**: Direct database connection via psycopg2 or asyncpg. Uses connection pooling.
- **Worker → Graph API**: HTTPS requests via the `requests` or `httpx` library. Authenticates using Azure AD client credentials (OAuth2 token).
- **Worker → AI APIs**: HTTPS requests via provider-specific SDKs (anthropic, openai, google-generativeai Python packages).
- **Worker → SMTP**: Standard SMTP connection for sending email notifications.

## Error Handling Philosophy

Every external call (Graph API, AI APIs, database writes, email sending) can fail. The system handles failures as follows:

1. **Transient failures** (network timeouts, rate limits, 5xx errors): Retry with exponential backoff, up to 3 attempts.
2. **Permanent failures** (404 file not found, 403 access denied, invalid file): Log the failure, record the event as "failed" with a reason in the database, do NOT retry.
3. **Processing failures** (text extraction crash, OCR error): Catch the exception, fall back to the next strategy in the pipeline (e.g., text extraction fails → try OCR → try multimodal → fall back to filename analysis). If all strategies fail, record as "extraction_failed" and notify analyst of the processing failure.
4. **Dead letter handling**: Jobs that fail all retries are recorded in the database with status "failed" and a description of the failure. A daily summary of failed jobs can be included in analyst notifications.

The system should NEVER silently drop a sharing event. Every event that arrives via webhook must result in either a recorded verdict or a recorded failure.

## Security Considerations

- **Temp files on tmpfs**: Sensitive file content exists only in RAM, never on persistent disk.
- **File deletion after processing**: The worker explicitly deletes temp files after recording the verdict, regardless of success or failure.
- **Cleanup safety net**: A background task in the worker periodically scans the tmpfs mount and deletes any files older than 30 minutes, catching any files missed by normal cleanup (e.g., if the worker crashes mid-processing).
- **No file content in logs**: Application logs contain only metadata (file ID, filename, event type, processing status, sensitivity rating). The AI's detailed summary is stored only in the database, not in log files.
- **AI API data agreements**: The organization has data processing agreements with all three AI providers. Nonetheless, the system minimizes data sent to AI APIs by extracting text locally and sending only text content rather than raw files when possible.
- **Azure AD app permissions**: The Graph API application uses the minimum required permissions: Files.Read.All and Sites.Read.All. It does NOT have write permissions.
- **Container isolation**: Each container runs with the minimum required privileges. The worker container does not need network-facing ports. Only the webhook listener exposes a port.

## Kubernetes Migration Notes

The Docker Compose setup is designed for straightforward Kubernetes migration:

- Each container becomes a Kubernetes Deployment.
- Redis and PostgreSQL become StatefulSets or are replaced by managed services (Azure Cache for Redis, Azure Database for PostgreSQL).
- Environment variables map to Kubernetes ConfigMaps and Secrets.
- The tmpfs mount maps to an emptyDir volume with medium: Memory in the pod spec.
- No shared Docker volumes between containers; all communication is via Redis/PostgreSQL.
- Health check endpoints in the webhook listener map to Kubernetes liveness/readiness probes.
- The worker's queue consumption loop naturally supports scaling to multiple replicas (Redis BLPOP is safe for concurrent consumers).
