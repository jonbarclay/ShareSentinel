# ShareSentinel

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Automated monitoring and sensitivity analysis for OneDrive/SharePoint file sharing activity.

## Overview

ShareSentinel is a containerized system that monitors OneDrive and SharePoint for anonymous and organization-wide sharing links. When a user creates a broad sharing link, ShareSentinel automatically downloads the shared file, extracts its text content, and submits it to an AI model for sensitivity analysis. If the AI detects sensitive content -- such as PII, FERPA-protected records, HIPAA data, or security credentials -- analysts are immediately notified with the file metadata, sensitivity findings, and a direct link to the shared item.

The system polls the Microsoft Graph Audit Log Query API every 15 minutes for `AnonymousLinkCreated` and `CompanyLinkCreated` events. Events are deduplicated via Redis and processed by a worker service that handles up to 5 jobs concurrently. The worker supports multiple processing paths depending on file type: text extraction for documents, multimodal analysis for images, audio/video transcription, and browser-based inspection for delegated content types like Loop, OneNote, and Whiteboard.

Beyond detection, ShareSentinel enforces a 180-day expiration policy on sharing links. Each anonymous or organization-wide link is enrolled in a lifecycle tracker that sends countdown notifications to file owners at scheduled milestones, then automatically removes the link via the Graph API at expiration. A web dashboard gives analysts a centralized view of events, AI verdicts, and sharing link status.

## Architecture

```
                    Microsoft Graph
                    Audit Log Query API
                           |
                           v
                  +------------------+
                  | lifecycle-cron   |
                  |  - audit poller  |----> Redis Queue ----> +----------+
                  |  - lifecycle     |                        |  worker  |
                  |    processor     |                        +----------+
                  +------------------+                          |  |  |
                           |                     +--------------+  |  +--------------+
                           |                     v                 v                 v
                           |               Graph API         AI Providers      PostgreSQL
                           |            (download files)   (Anthropic/OpenAI/   (events,
                           |                                    Gemini)         verdicts)
                           v                                      |
                         SMTP  <----------------------------------+
                    (countdown emails,                        (analyst alerts)
                     analyst alerts)
                                            +-------------+
                                            |  dashboard  |
                                            | (React +    |----> PostgreSQL
                                            |  FastAPI)   |
                                            +-------------+
                                                  |
                                                  v
                                            Inspection Queue
                                          (Playwright browser
                                           screenshots for
                                           delegated content)
```

## Containers

| Container | Description |
|---|---|
| **lifecycle-cron** | Runs two concurrent loops: (1) audit log poller that queries the Graph API every 15 minutes for new sharing events and pushes them to the Redis queue, and (2) lifecycle processor that checks sharing link expiry milestones daily, sends countdown notifications, and removes expired links. |
| **worker** | Python service that pulls jobs from Redis (up to 5 concurrently), orchestrates the full processing pipeline: metadata pre-screen, download, text extraction, AI analysis, lifecycle enrollment, notification, and cleanup. |
| **dashboard** | React + FastAPI web UI for analysts to review events, verdicts, statistics, and process delegated content types via the Inspection Queue. |
| **redis** | Job queue, deduplication cache, and rate limiting state. |
| **postgres** | Event records, AI verdicts, analyst dispositions, sharing link lifecycle tracking, and audit poll state. |

## Quick Start

```bash
git clone https://github.com/your-org/ShareSentinel.git
cd ShareSentinel
cp .env.example .env
# Edit .env with your Azure AD, AI API keys, SMTP settings
docker compose up --build -d
```

## Configuration

All configuration is via environment variables. See [.env.example](.env.example) for the full list.

| Category | Key Variables | Description |
|---|---|---|
| **Azure AD** | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | App registration for Microsoft Graph API access. Requires `Files.Read.All`, `Sites.Read.All`, `Sites.FullControl.All`, and `AuditLogsQuery.Read.All` permissions. |
| **AI Providers** | `AI_PROVIDER`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` | AI provider selection and API keys. Supports `anthropic`, `openai`, and `gemini`. |
| **SMTP** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Email server for analyst alerts and lifecycle countdown notifications. |
| **PostgreSQL** | `DATABASE_URL` | Connection string for the PostgreSQL database. |
| **Redis** | `REDIS_URL` | Connection string for the Redis instance. |

## Sensitivity Categories

The AI returns detected sensitivity categories organized into tiers. Escalation is deterministic: any Tier 1 or Tier 2 category triggers analyst notification.

| Tier | Level | Categories |
|---|---|---|
| **Tier 1** | Urgent | `pii_government_id`, `pii_financial`, `ferpa`, `hipaa`, `security_credentials` |
| **Tier 2** | Normal | `hr_personnel`, `legal_confidential`, `pii_contact` |
| **Tier 3** | No escalation | `coursework`, `casual_personal`, `none` |

## Processing Paths

| Scenario | Processing Path |
|---|---|
| **Text-extractable files** (PDF, DOCX, XLSX, PPTX, CSV, TXT) | Text extraction via PyMuPDF, python-docx, openpyxl, python-pptx, or direct read, then AI text analysis. |
| **Images and scanned documents** (PNG, JPG, TIFF, scanned PDFs) | OCR via Tesseract, or resize/compress and send to AI as multimodal input. |
| **Audio and video** (MP3, WAV, MP4, MOV, etc.) | Transcription pipeline, then AI text analysis on the transcript. |
| **Delegated content** (Loop, OneNote, Whiteboard) | Parked as `pending_manual_inspection`, processed via the dashboard's Inspection Queue using Playwright browser screenshots with saved authentication cookies, then multimodal AI analysis. |
| **Oversized files** (> 50 MB) or **excluded types** (binaries) | Filename, file path, file size, and sharing metadata sent to AI for assessment. |
| **Folder shares** | Child files enumerated and analyzed individually; folder also flagged for analyst review (future files inherit sharing scope). |

## Sharing Link Lifecycle

All anonymous and organization-wide sharing links are enrolled in a 180-day lifecycle tracker. Links with a Microsoft-set expiration date are marked `ms_managed` and exempt from countdown notifications.

| Days Elapsed | Action | Days Remaining |
|---|---|---|
| 120 | First countdown email to file owner | 60 |
| 150 | Second countdown email | 30 |
| 165 | Third countdown email | 15 |
| 173 | Urgent reminder email | 7 |
| 178 | Final warning email | 2 |
| 180 | Remove link via Graph API + confirmation email | 0 |

The lifecycle processor runs daily in the lifecycle-cron container.

## Key Design Decisions

- **Text extraction first, multimodal as fallback.** Text-based AI calls are significantly cheaper and often more accurate. Multimodal analysis is reserved for actual images and documents where text extraction fails.
- **Files never touch persistent disk.** All downloads go to a tmpfs (RAM-backed) mount in the worker container. Files are explicitly deleted after processing and automatically cleaned on container restart.
- **AI provider is swappable via configuration.** Switching between Anthropic Claude, OpenAI, and Google Gemini requires only a configuration change. All providers implement the same interface and return the same structured output format.
- **Deterministic escalation.** Any Tier 1 or Tier 2 sensitivity category triggers analyst notification. There is no configurable threshold or scoring -- the system either escalates or it does not.
- **Folder shares are always flagged.** Even if all current child files are benign, the broad sharing scope means future files will inherit it, so analysts are always notified.

## Documentation

Detailed specifications for each component are in the [`docs/`](docs/) directory:

1. [Architecture Overview](docs/01-architecture-overview.md) -- System architecture, data flow, container layout, technology rationale
2. [Event Ingestion Service](docs/02-webhook-listener-service.md) -- Audit log poller, Graph API query flow, record-to-job mapping
3. [File Processing Pipeline](docs/03-file-processing-pipeline.md) -- Worker orchestration: metadata pre-screen, download, classification, routing
4. [Text Extraction Module](docs/04-text-extraction-module.md) -- Extraction strategies per file type, sampling logic, OCR fallback
5. [Image Preprocessing Module](docs/05-image-preprocessing-module.md) -- Image resizing, compression, scanned PDF rendering, multimodal preparation
6. [AI Provider Abstraction](docs/06-ai-provider-abstraction.md) -- Provider interface, prompt management, structured output parsing, cost tracking
7. [Database Schema](docs/07-database-schema.md) -- PostgreSQL tables, indexes, audit logging, migration strategy
8. [Notification Service](docs/08-notification-service.md) -- Email alerting, Jira integration, notification interface design
9. [Configuration & Deployment](docs/09-configuration-deployment.md) -- Docker Compose setup, environment variables, secrets, health checks
10. [Testing & Calibration](docs/10-testing-calibration.md) -- Test strategy, AI benchmarking, sample file creation, validation methodology

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
