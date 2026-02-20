# ShareSentinel

Automated monitoring system for OneDrive and SharePoint file sharing activity. Detects sensitive content in broadly shared files and alerts security analysts.

## How It Works

1. **Splunk** fires a webhook when a user creates an anonymous or organization-wide sharing link
2. **Webhook Listener** validates, deduplicates, and queues the event
3. **Worker** processes the job through a 12-step pipeline:
   - Fetches file metadata via Microsoft Graph API
   - Classifies the file type and applies exclusion rules
   - Downloads the file to a RAM-backed tmpfs mount
   - Extracts text content (PDF, DOCX, XLSX, PPTX, CSV, TXT, OCR)
   - Sends content to an AI model for sensitivity analysis
   - Alerts analysts if the file is rated 4 or 5 out of 5
   - Deletes the temporary file

Folder shares are always flagged for analyst review — their risk grows over time as new files are added.

## Architecture

```
Splunk → webhook-listener (FastAPI) → Redis Queue → worker
                                                      ├─ Graph API (metadata + download)
                                                      ├─ Text Extraction (8 extractors + OCR)
                                                      ├─ AI Analysis (Anthropic / OpenAI / Gemini)
                                                      ├─ PostgreSQL (events, verdicts, audit log)
                                                      └─ Notifications (Email / Jira)
```

Four Docker containers: `webhook-listener`, `worker`, `redis`, `postgres`

## Quick Start

```bash
# 1. Configure
cp .env.example .env
# Edit .env with your credentials (Azure AD, AI API keys, SMTP, etc.)

# 2. Start
docker compose up --build -d

# 3. Verify
curl http://localhost:8000/health

# 4. Test
bash scripts/test_webhook.sh
```

## Project Structure

```
share-sentinel/
├── docker-compose.yml
├── .env.example
├── config/
│   ├── file_types.yml                    # Extension classification + sensitivity keywords
│   ├── prompt_templates/
│   │   └── sensitivity_analysis.txt      # AI prompt template (3 modes)
│   └── notification_templates/
│       └── analyst_alert.html            # Email template
├── services/
│   ├── webhook-listener/                 # FastAPI service
│   │   ├── app/
│   │   │   ├── main.py                   # /webhook/splunk POST, /health GET
│   │   │   ├── config.py
│   │   │   ├── models.py                 # Pydantic models
│   │   │   ├── validation.py
│   │   │   ├── deduplication.py          # Redis SET NX
│   │   │   └── queue.py                  # Redis RPUSH
│   │   └── tests/
│   └── worker/                           # Queue consumer
│       ├── app/
│       │   ├── main.py                   # BLPOP loop + heartbeat
│       │   ├── config.py
│       │   ├── logging_config.py
│       │   ├── pipeline/
│       │   │   ├── orchestrator.py       # 12-step process_job()
│       │   │   ├── metadata.py           # Graph API pre-screen
│       │   │   ├── classifier.py         # File type routing
│       │   │   ├── downloader.py         # Streaming download to tmpfs
│       │   │   ├── hasher.py             # SHA-256 dedup
│       │   │   ├── cleanup.py            # Temp file deletion
│       │   │   └── retry.py              # Exponential backoff
│       │   ├── extraction/               # 8 text extractors + OCR + image preprocessing
│       │   ├── ai/                       # 3 providers + prompt manager + response parser
│       │   ├── notifications/            # Email (SMTP) + Jira
│       │   ├── database/                 # asyncpg repositories + migrations
│       │   └── graph_api/                # Azure AD auth + Graph API client
│       └── tests/
├── scripts/
│   ├── init_db.sql
│   └── test_webhook.sh
└── docs/                                 # Detailed spec documents
```

## Configuration

All configuration is via environment variables. See [.env.example](.env.example) for the full list.

Key settings:

| Variable | Description | Default |
|-|-|-|
| `AI_PROVIDER` | AI provider: `anthropic`, `openai`, `gemini` | `anthropic` |
| `SENSITIVITY_THRESHOLD` | Min rating to trigger alerts (1-5) | `4` |
| `MAX_FILE_SIZE_BYTES` | Max file size to download | `52428800` (50MB) |
| `NOTIFICATION_CHANNELS` | Comma-separated: `email`, `jira` | `email` |

## Processing Paths

| Scenario | Path |
|-|-|
| Folder shared | Immediate analyst alert (no AI analysis) |
| Document (PDF, DOCX, XLSX, PPTX) | Text extraction → AI text analysis |
| CSV/TXT/JSON/XML/HTML | Direct text read → AI text analysis |
| Scanned PDF | Text extraction fails → OCR → AI text analysis |
| OCR fails on PDF | Render pages as images → AI multimodal analysis |
| Image file (PNG, JPG, etc.) | Resize/compress → AI multimodal analysis |
| Archive (ZIP) | List manifest → AI text analysis on filenames |
| Excluded type (video, audio, binary) | Filename/path-only AI analysis |
| File > 50MB | Filename/path-only AI analysis |
| Same file hash within 30 days | Reuse previous verdict (skip AI) |

## AI Sensitivity Rating Scale

| Rating | Meaning | Action |
|-|-|-|
| 1 | No sensitive information | None |
| 2 | Minor sensitivity | None |
| 3 | Moderate / unknown | None (default for filename-only) |
| 4 | High sensitivity | Analyst notified |
| 5 | Critical sensitivity (PII, medical, tax) | Analyst notified |

## Design Decisions

- **Text extraction first, multimodal as fallback** — text-based AI calls are 10-50x cheaper
- **Files never touch persistent disk** — tmpfs (RAM-backed) with automatic cleanup
- **AI provider is swappable** — change `AI_PROVIDER` env var, no code changes
- **Deduplication at two levels** — webhook dedup (Redis, 24h TTL) and file content dedup (SHA-256 hash, 30-day reuse)
- **Folder shares always flagged** — future files inherit the sharing scope

## Operational Notes

- **Volume**: Designed for < 100 sharing events/day
- **Latency tolerance**: High — multi-day delays are acceptable
- **Worker heartbeat**: Writes to `sharesentinel:worker:heartbeat` in Redis every 60s
- **Stale file cleanup**: Background task scans tmpfs every 5 minutes, removes files older than 30 minutes
- **Structured logging**: All services output JSON logs to stdout

## Documentation

Detailed specifications for each component are in the `docs/` directory:

1. [Architecture Overview](docs/01-architecture-overview.md)
2. [Webhook Listener Service](docs/02-webhook-listener-service.md)
3. [File Processing Pipeline](docs/03-file-processing-pipeline.md)
4. [Text Extraction Module](docs/04-text-extraction-module.md)
5. [Image Preprocessing Module](docs/05-image-preprocessing-module.md)
6. [AI Provider Abstraction](docs/06-ai-provider-abstraction.md)
7. [Database Schema](docs/07-database-schema.md)
8. [Notification Service](docs/08-notification-service.md)
9. [Configuration & Deployment](docs/09-configuration-deployment.md)
10. [Testing & Calibration](docs/10-testing-calibration.md)
