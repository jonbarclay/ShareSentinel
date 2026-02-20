# ShareSentinel - CLAUDE.md

## Project Overview

ShareSentinel is a containerized system that monitors OneDrive and SharePoint file sharing activity, automatically analyzing shared files for sensitive content, and alerting human analysts when risky files are detected.

The system receives webhook alerts from Splunk (triggered by Azure OneDrive/SharePoint sharing audit logs), downloads shared files via the Microsoft Graph API, preprocesses them, submits them to an AI model for sensitivity analysis, and notifies analysts of high-risk findings.

## Core Workflow Summary

1. Splunk fires a webhook when a user creates an anonymous link or organization-wide sharing link (edit or view) in OneDrive or SharePoint.
2. The webhook listener validates, deduplicates, and queues the event.
3. A worker process picks up the job, determines if the shared item is a file or folder.
4. **Folders**: Immediately flag for analyst review (folders with broad sharing are inherently risky because future files added inherit the sharing scope).
5. **Files**: Download via Graph API, preprocess (text extraction, OCR, image compression), send to AI for sensitivity analysis.
6. If the AI rates the file sensitivity at 4 or 5 (out of 5), notify analysts with the file metadata and sharing link.
7. Delete the temporary file after processing.

## Architecture

The system runs as Docker Compose services on an Ubuntu server, with a future migration path to Kubernetes.

### Containers

- **webhook-listener**: FastAPI service that receives Splunk webhooks, validates payloads, deduplicates via Redis, and pushes jobs onto the Redis queue.
- **worker**: Python service that pulls jobs from Redis, orchestrates the full processing pipeline (metadata pre-screen, download, text extraction, AI analysis, notification, cleanup).
- **redis**: Job queue, deduplication cache, and rate limiting state.
- **postgres**: Event records, AI verdicts, analyst dispositions, audit log, configuration.

### Key Technology Choices

- **Language**: Python 3.12+
- **Web framework**: FastAPI (webhook listener)
- **Queue**: Redis (using Redis lists or streams for job queuing)
- **Database**: PostgreSQL 16
- **File storage**: tmpfs (RAM-backed mount in the worker container; files never touch persistent disk)
- **Text extraction**: PyMuPDF (PDFs), python-docx (Word), openpyxl (Excel), python-pptx (PowerPoint), Tesseract OCR (scanned documents)
- **Image processing**: Pillow (resizing/compression)
- **AI providers**: Anthropic Claude API, OpenAI API, Google Gemini API (abstracted behind a common interface)
- **Notifications**: Phase 1 = Email (SMTP), Phase 2 = Jira ticket creation

## Detailed Planning Documents

The following documents in the `docs/` directory contain detailed specifications for each component. Each document is self-contained enough to be used as a standalone implementation guide for a sub-agent.

| Document | Description |
|----------|-------------|
| [docs/01-architecture-overview.md](docs/01-architecture-overview.md) | Full system architecture, data flow diagrams, container layout, technology rationale |
| [docs/02-webhook-listener-service.md](docs/02-webhook-listener-service.md) | FastAPI webhook receiver, payload validation, deduplication, queue integration |
| [docs/03-file-processing-pipeline.md](docs/03-file-processing-pipeline.md) | Master orchestration logic for the worker: metadata pre-screen, download, classification, routing |
| [docs/04-text-extraction-module.md](docs/04-text-extraction-module.md) | Text extraction strategies for every supported file type, sampling logic, OCR fallback |
| [docs/05-image-preprocessing-module.md](docs/05-image-preprocessing-module.md) | Image resizing, compression, scanned PDF page rendering, multimodal preparation |
| [docs/06-ai-provider-abstraction.md](docs/06-ai-provider-abstraction.md) | Provider interface, prompt management, structured output parsing, cost tracking, provider switching |
| [docs/07-database-schema.md](docs/07-database-schema.md) | PostgreSQL tables, indexes, audit logging, migration strategy |
| [docs/08-notification-service.md](docs/08-notification-service.md) | Phase 1 email alerting, Phase 2 Jira integration, notification interface design |
| [docs/09-configuration-deployment.md](docs/09-configuration-deployment.md) | Docker Compose setup, environment variables, secrets, health checks, tmpfs config, Kubernetes migration notes |
| [docs/10-testing-calibration.md](docs/10-testing-calibration.md) | Test strategy, AI provider benchmarking, sample sensitive file creation, validation methodology |

## Project Structure

```
share-sentinel/
├── CLAUDE.md                          # This file - primary project reference
├── docker-compose.yml                 # Container orchestration
├── .env.example                       # Environment variable template
├── docs/                              # Planning documents (detailed specs)
│   ├── 01-architecture-overview.md
│   ├── 02-webhook-listener-service.md
│   ├── 03-file-processing-pipeline.md
│   ├── 04-text-extraction-module.md
│   ├── 05-image-preprocessing-module.md
│   ├── 06-ai-provider-abstraction.md
│   ├── 07-database-schema.md
│   ├── 08-notification-service.md
│   ├── 09-configuration-deployment.md
│   └── 10-testing-calibration.md
├── services/
│   ├── webhook-listener/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── app/
│   │   │   ├── __init__.py
│   │   │   ├── main.py                # FastAPI app entry point
│   │   │   ├── config.py              # Configuration loading
│   │   │   ├── models.py              # Pydantic models for webhook payloads
│   │   │   ├── validation.py          # Payload validation logic
│   │   │   ├── deduplication.py        # Redis-based dedup
│   │   │   └── queue.py               # Redis queue push logic
│   │   └── tests/
│   │       ├── test_validation.py
│   │       ├── test_deduplication.py
│   │       └── test_webhook.py
│   └── worker/
│       ├── Dockerfile
│       ├── requirements.txt
│       ├── app/
│       │   ├── __init__.py
│       │   ├── main.py                # Worker entry point (queue consumer loop)
│       │   ├── config.py              # Configuration loading
│       │   ├── pipeline/
│       │   │   ├── __init__.py
│       │   │   ├── orchestrator.py    # Master pipeline logic
│       │   │   ├── metadata.py        # Graph API metadata pre-screen
│       │   │   ├── downloader.py      # Graph API file download
│       │   │   ├── classifier.py      # File type classification and routing
│       │   │   ├── hasher.py          # File hash computation and dedup check
│       │   │   └── cleanup.py         # Temp file deletion
│       │   ├── extraction/
│       │   │   ├── __init__.py
│       │   │   ├── base.py            # Base extractor interface
│       │   │   ├── pdf_extractor.py   # PDF text extraction (PyMuPDF)
│       │   │   ├── docx_extractor.py  # Word document extraction
│       │   │   ├── xlsx_extractor.py  # Excel extraction
│       │   │   ├── pptx_extractor.py  # PowerPoint extraction
│       │   │   ├── csv_extractor.py   # CSV/TSV extraction
│       │   │   ├── text_extractor.py  # Plain text file extraction
│       │   │   ├── image_preprocessor.py  # Image resize/compression
│       │   │   ├── ocr_extractor.py   # Tesseract OCR fallback
│       │   │   └── archive_extractor.py   # ZIP/RAR manifest listing
│       │   ├── ai/
│       │   │   ├── __init__.py
│       │   │   ├── base_provider.py   # Abstract provider interface
│       │   │   ├── anthropic_provider.py
│       │   │   ├── openai_provider.py
│       │   │   ├── gemini_provider.py
│       │   │   ├── prompt_manager.py  # Prompt template loading and rendering
│       │   │   ├── response_parser.py # Structured output parsing
│       │   │   └── cost_tracker.py    # Token/cost logging
│       │   ├── notifications/
│       │   │   ├── __init__.py
│       │   │   ├── base_notifier.py   # Abstract notifier interface
│       │   │   ├── email_notifier.py  # SMTP email (Phase 1)
│       │   │   └── jira_notifier.py   # Jira ticket creation (Phase 2)
│       │   ├── database/
│       │   │   ├── __init__.py
│       │   │   ├── connection.py      # PostgreSQL connection management
│       │   │   ├── models.py          # SQLAlchemy or raw SQL models
│       │   │   ├── repositories.py    # Data access methods
│       │   │   └── migrations/        # Schema migrations
│       │   │       └── 001_initial.sql
│       │   └── graph_api/
│       │       ├── __init__.py
│       │       ├── auth.py            # Azure AD authentication
│       │       ├── client.py          # Graph API client wrapper
│       │       └── sharing.py         # Sharing link retrieval
│       └── tests/
│           ├── test_pipeline.py
│           ├── test_extraction/
│           ├── test_ai/
│           └── test_notifications/
├── config/
│   ├── prompt_templates/
│   │   └── sensitivity_analysis.txt   # The AI prompt template (configurable)
│   ├── file_types.yml                 # File type classification config
│   └── notification_templates/
│       └── analyst_alert.html         # Email template for analyst notifications
└── scripts/
    ├── init_db.sql                    # Database initialization
    └── test_webhook.sh                # Script to send test webhooks
```

## Critical Design Decisions

### Files are ALWAYS text-extracted first
For every file type that supports text extraction (PDF, DOCX, XLSX, PPTX, CSV, TXT), always extract text before sending to the AI. Send extracted text as a text-based prompt (cheaper, faster, often more accurate). Only fall back to multimodal (image-based) analysis when text extraction fails (scanned documents, actual images). This minimizes API costs significantly.

### Multimodal is the fallback, not the default
The multimodal path (sending images to the AI) is reserved for: actual image files (PNG, JPG, TIFF, etc.), scanned PDFs where text extraction and OCR both fail, and PDF pages that contain primarily visual content. Everything else goes through text extraction.

### Folder shares are always flagged for human review
When a folder (not a file) is shared with anonymous or org-wide access, the system does NOT attempt AI analysis. It immediately creates an alert for a human analyst. The reasoning: a shared folder's risk grows over time as new files are added, and AI can't predict what will be added in the future.

### Temporary files never touch persistent disk
All downloaded files are stored on a tmpfs mount (RAM-backed filesystem) in the worker container. This ensures files are automatically cleaned up on container restart and never persist to disk. The worker also explicitly deletes files after processing.

### AI provider is swappable via configuration
The system uses an abstract provider interface. Switching between Anthropic, OpenAI, and Google Gemini requires only a configuration change, not a code change. All providers implement the same interface and return the same structured output format.

### Filename/path analysis is the fallback for unprocessable files
Files that are too large to download, are excluded types (video, audio, binaries), or fail all extraction methods still get analyzed. The AI receives the filename, file path, file size, and sharing metadata and provides a sensitivity assessment based solely on that information.

## Important Constraints

- **Volume**: Less than 100 sharing events per day on average.
- **Latency tolerance**: High. Even multi-day processing delays are acceptable.
- **File size download limit**: 50MB. Files larger than this get filename/path analysis only.
- **Text content limit for AI**: 100KB of extracted text (~25K tokens). Content exceeding this is sampled.
- **Image size limit for multimodal**: Resize to longest edge 1600px, compress to JPEG quality 85, target under 1MB.
- **Sensitivity threshold for alerts**: Rating of 4 or 5 (out of 5) triggers analyst notification.
- **Excluded file types**: Video (.mp4, .mov, .avi, .mkv, .wmv), Audio (.mp3, .wav, .m4a, .aac, .flac), Binaries (.exe, .dll, .bin), and other non-document types defined in config/file_types.yml.

## Environment and Authentication

- **Microsoft Graph API**: Azure AD app registration with application permissions for Files.Read.All and Sites.Read.All. Uses client credentials flow (client_id + client_secret + tenant_id).
- **AI APIs**: API keys for each provider stored as environment variables or Docker secrets.
- **SMTP**: Configured for analyst email notifications.
- **PostgreSQL**: Connection string via environment variable.
- **Redis**: Connection string via environment variable.
