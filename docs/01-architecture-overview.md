# 01 - Architecture Overview

## System Purpose

ShareSentinel monitors file sharing activity in OneDrive and SharePoint. When a user creates an anonymous link or an organization-wide sharing link, the system automatically evaluates whether the shared file contains sensitive content. If the content appears risky, human analysts are notified so they can reach out to the user. Sharing links are also enrolled in a 180-day lifecycle tracker that sends countdown notifications to the file owner and automatically removes the link at expiration.

## Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL SYSTEMS                              │
│                                                                         │
│  SharePoint/OneDrive ──► Microsoft 365 Audit Logs                      │
│                                                                         │
│  Microsoft Graph API (audit log queries + file metadata + download)     │
│  AI APIs (Anthropic / OpenAI / Gemini)                                  │
│  SMTP Server (analyst + user email notifications)                       │
│  Jira API (Phase 2 - ticket creation)                                   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         SHARESENTINEL SYSTEM                            │
│                                                                         │
│  ┌──────────────────────┐                                              │
│  │ Lifecycle Cron        │                                              │
│  │                       │                                              │
│  │ ┌───────────────────┐│   ┌───────┐    ┌──────────────────────────┐  │
│  │ │ Audit Log Poller  ││──►│ Redis │───►│ Worker                   │  │
│  │ │ (every 15 min)    ││   │       │    │                          │  │
│  │ └───────────────────┘│   │ Queue │    │ ┌──────────────────────┐ │  │
│  │ ┌───────────────────┐│   │ Dedup │    │ │ Pipeline Orchestrator│ │  │
│  │ │ Lifecycle         ││   │ Cache │    │ │                      │ │  │
│  │ │ Processor (daily) ││   │       │    │ │ 1. Record Event      │ │  │
│  │ │ - Notifications   ││   └───────┘    │ │ 2. Classify Item     │ │  │
│  │ │ - Link removal    ││               │ │ 3. Metadata + Enroll │ │  │
│  │ └───────────────────┘│               │ │ 4. Text Extraction   │ │  │
│  └──────────────────────┘               │ │ 5. AI Analysis       │ │  │
│                                          │ │ 6. Verdict Recording │ │  │
│  ┌──────────────────────┐               │ │ 7. Notification      │ │  │
│  │ Dashboard             │               │ │ 8. Cleanup           │ │  │
│  │ (React + FastAPI)     │               │ └──────────────────────┘ │  │
│  └──────────────────────┘               └──────────────┬───────────┘  │
│                                                         │              │
│                                              ┌──────────▼──────────┐  │
│                                              │  PostgreSQL          │  │
│                                              │                      │  │
│                                              │ - Events             │  │
│                                              │ - Verdicts           │  │
│                                              │ - File Hashes        │  │
│                                              │ - Sharing Link       │  │
│                                              │   Lifecycle          │  │
│                                              │ - Audit Poll State   │  │
│                                              │ - Audit Log          │  │
│                                              └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Container Layout

### Container 1: lifecycle-cron

**Purpose**: Runs two concurrent background loops:
1. **Audit Log Poller** — queries the Microsoft Graph Audit Log Query API every 15 minutes for new `AnonymousLinkCreated` and `CompanyLinkCreated` events. Deduplicates via Redis and pushes new jobs onto the Redis queue for the worker.
2. **Lifecycle Processor** — checks sharing link expiry milestones daily, sends countdown notifications to file owners, and removes expired sharing links via the Graph API at the 180-day mark.

**Technology**: Python 3.12, asyncpg, httpx, redis.asyncio

**Connections**: Microsoft Graph API (audit log queries + sharing link removal), Redis (dedup + queue push), PostgreSQL (poll state + lifecycle tracking), SMTP (lifecycle notifications).

**Stateless**: Yes. Polling state is stored in PostgreSQL.

### Container 2: worker

**Purpose**: Consumes jobs from the Redis queue and executes the full file processing pipeline. Processes up to 5 jobs concurrently (configurable via `MAX_CONCURRENT_JOBS`).

**Technology**: Python 3.12, with libraries for text extraction (PyMuPDF, python-docx, openpyxl, python-pptx), OCR (Tesseract via pytesseract), image processing (Pillow), and AI API clients.

**Mounts**: tmpfs mount at `/tmp/sharesentinel` for temporary file storage (RAM-backed, never persists to disk).

**Connections**: Redis (queue consumption), PostgreSQL (verdict storage, audit logging), Microsoft Graph API (file download + sharing permissions), AI APIs (analysis), SMTP (notifications).

**Stateless**: Yes, aside from temporary files in tmpfs which are cleaned up after each job.

**Resource requirements**: Moderate. Needs enough RAM for tmpfs file storage (allocate 256MB for tmpfs, sufficient for the largest files we'll download). CPU usage spikes during text extraction and OCR.

**System dependencies**: Tesseract OCR must be installed in the Docker image (`apt-get install tesseract-ocr`).

### Container 3: dashboard

**Purpose**: Web UI for analysts to review events, verdicts, and statistics.

**Technology**: React (Vite) frontend served by a FastAPI backend.

**Connections**: PostgreSQL (read events, verdicts, statistics).

### Container 4: redis

**Purpose**: Job queue (Redis list), deduplication cache (Redis SET with TTL), and general caching.

**Technology**: Redis 7+ (official Docker image).

**Persistence**: Optional. AOF enabled for surviving container restarts. If Redis loses its state, the dedup cache is cleared (some events might be re-processed, which is harmless) and any in-flight queue jobs are lost (they'll be picked up on the next audit log poll cycle).

**Resource requirements**: Minimal. The queue will never hold more than a few hundred items.

### Container 5: postgres

**Purpose**: Persistent storage for event records, AI verdicts, file hashes, sharing link lifecycle tracking, audit poll state, and operational audit logs.

**Technology**: PostgreSQL 16 (official Docker image).

**Persistence**: Required. Uses a Docker volume for data persistence.

**Resource requirements**: Minimal. Database will be small (< 100 records/day, each a few KB).

## Technology Rationale

**Direct Audit Log Polling**: The Microsoft Graph Audit Log Query API provides sharing event data directly. The 15-minute polling interval is sufficient given the system's high latency tolerance, and polling avoids the complexity of webhook infrastructure.

**Redis over RabbitMQ**: Redis is simpler to operate, has a smaller footprint, and provides everything we need (basic queue, deduplication cache, TTL-based key expiry). RabbitMQ would be overkill for < 100 jobs/day.

**PostgreSQL over SQLite**: PostgreSQL is the better choice for a multi-container setup. Multiple services need database access (worker, dashboard, lifecycle-cron). PostgreSQL also has a cleaner migration path to Kubernetes (managed database services).

**tmpfs over disk-based temp storage**: Files potentially contain the most sensitive content in the organization. tmpfs ensures they exist only in RAM, are never written to persistent disk, and are automatically purged on container restart.

**Tesseract OCR over cloud OCR services**: Keeps OCR processing local (no additional data leaving the system), is free, and is sufficient quality for cleanly scanned documents.

## Communication Patterns

All inter-container communication uses the Docker Compose internal network.

- **Lifecycle Cron → Graph API**: HTTPS requests via httpx to the beta audit log query endpoint and v1.0 sharing permissions endpoint.
- **Lifecycle Cron → Redis**: Direct Redis client connection. RPUSH job payloads and SET NX for dedup.
- **Worker → Redis**: Direct Redis client connection. Blocking pop (BLPOP) from the Redis list to consume jobs.
- **Worker → PostgreSQL**: Direct database connection via asyncpg with connection pooling.
- **Worker → Graph API**: HTTPS requests via httpx. Authenticates using Azure AD client credentials (OAuth2 token).
- **Worker → AI APIs**: HTTPS requests via provider-specific SDKs (anthropic, openai, google-generativeai Python packages).
- **Worker → SMTP**: Standard SMTP connection for sending email notifications.
- **Dashboard → PostgreSQL**: Direct database connection for read queries.

## Error Handling Philosophy

Every external call (Graph API, AI APIs, database writes, email sending) can fail. The system handles failures as follows:

1. **Transient failures** (network timeouts, rate limits, 5xx errors): Retry with exponential backoff, up to 3 attempts.
2. **Permanent failures** (404 file not found, 403 access denied, invalid file): Log the failure, record the event as "failed" with a reason in the database, do NOT retry.
3. **Processing failures** (text extraction crash, OCR error): Catch the exception, fall back to the next strategy in the pipeline (e.g., text extraction fails → try OCR → try multimodal → fall back to filename analysis). If all strategies fail, record as "extraction_failed" and notify analyst of the processing failure.
4. **Dead letter handling**: Jobs that fail all retries are recorded in the database with status "failed" and a description of the failure. A daily summary of failed jobs can be included in analyst notifications.

The system should NEVER silently drop a sharing event. Every event that enters the queue must result in either a recorded verdict or a recorded failure.

## Sharing Link Lifecycle (180-Day Expiration)

When a sharing event is processed, the worker enrolls each anonymous/org-wide sharing permission into the `sharing_link_lifecycle` table. Links with a Microsoft-set `expirationDateTime` are marked `ms_managed` and exempt from countdown notifications and removal.

**Milestone schedule for active (non-MS-managed) links:**

| Days since creation | Action | Days remaining |
|-|-|-|
| 120 | First countdown email to file owner | 60 |
| 150 | Second countdown email | 30 |
| 165 | Third countdown email | 15 |
| 173 | Urgent reminder email | 7 |
| 178 | Final warning email | 2 |
| 180 | Remove sharing link via Graph API + removal confirmation email | 0 |

The lifecycle processor runs daily in the lifecycle-cron container. It queries for rows where `link_created_at + N days <= NOW()` and the corresponding milestone column is NULL. See doc 11 for full details.

## Security Considerations

- **Temp files on tmpfs**: Sensitive file content exists only in RAM, never on persistent disk.
- **File deletion after processing**: The worker explicitly deletes temp files after recording the verdict, regardless of success or failure.
- **Cleanup safety net**: A background task in the worker periodically scans the tmpfs mount and deletes any files older than 30 minutes, catching any files missed by normal cleanup.
- **No file content in logs**: Application logs contain only metadata (file ID, filename, event type, processing status, sensitivity categories). The AI's detailed summary is stored only in the database, not in log files.
- **AI API data agreements**: The organization has data processing agreements with all three AI providers. Nonetheless, the system minimizes data sent to AI APIs by extracting text locally and sending only text content rather than raw files when possible.
- **Azure AD app permissions**: The Graph API application uses `Files.Read.All`, `Sites.Read.All`, and `AuditLogsQuery.Read.All`. Write permissions are limited to `Sites.FullControl.All` (required for sharing link removal at the 180-day mark).
- **Container isolation**: Each container runs with the minimum required privileges. Only the dashboard exposes an HTTP port.

## Kubernetes Migration Notes

The Docker Compose setup is designed for straightforward Kubernetes migration:

- Each container becomes a Kubernetes Deployment.
- Redis and PostgreSQL become StatefulSets or are replaced by managed services (Azure Cache for Redis, Azure Database for PostgreSQL).
- Environment variables map to Kubernetes ConfigMaps and Secrets.
- The tmpfs mount maps to an emptyDir volume with medium: Memory in the pod spec.
- No shared Docker volumes between containers; all communication is via Redis/PostgreSQL.
- Health check endpoints in the dashboard map to Kubernetes liveness/readiness probes.
- The worker's queue consumption loop naturally supports scaling to multiple replicas (Redis BLPOP is safe for concurrent consumers).
