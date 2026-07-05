# ShareSentinel - CLAUDE.md

## Project Overview

ShareSentinel is a containerized system that monitors OneDrive and SharePoint file sharing activity, automatically analyzing shared files for sensitive content, and alerting human analysts when risky files are detected. It also enforces a 180-day expiration policy on anonymous and organization-wide sharing links, sending countdown notifications to file owners and automatically removing links at expiration. Additionally, ShareSentinel enforces site-level policies: it ensures only explicitly allow-listed SharePoint sites can have anonymous sharing enabled, and only allow-listed M365 groups can have Public visibility.

The system polls the Microsoft Graph Audit Log Query API for sharing events, downloads shared files via the Microsoft Graph API, preprocesses them, submits them to an AI model for sensitivity analysis, and notifies analysts of high-risk findings. A daily site policy scanner enforces sharing capability and group visibility policies across all SharePoint sites and M365 groups.

## Core Workflow Summary

1. The audit log poller (in the lifecycle-cron container) queries the Microsoft Graph Audit Log Query API every 15 minutes for `AnonymousLinkCreated` and `CompanyLinkCreated` events.
2. New events are deduplicated via Redis and pushed onto the Redis job queue.
3. The worker picks up jobs (up to 5 concurrently), determines if the shared item is a file or folder.
4. **Folders**: Enumerate folder contents via Graph API, download and analyze each child file individually, then flag the folder for analyst review (folders with broad sharing are inherently risky because future files added inherit the sharing scope).
5. **Files**: Download via Graph API, preprocess (text extraction, OCR, image compression), send to AI for sensitivity analysis.
6. **Delegated content types** (Loop, OneNote, Whiteboard): These cannot be fetched via the application-level Graph API. They are parked as `pending_manual_inspection` and processed via the dashboard's Inspection Queue, which uses Playwright browser screenshots of sharing URLs with saved authentication cookies, followed by multimodal AI analysis.
7. If the AI detects Tier 1 or Tier 2 sensitivity categories (e.g., PII, FERPA, HIPAA), notify analysts with the file metadata and sharing link.
8. Enroll each anonymous/org-wide sharing permission in the 180-day lifecycle tracker.
9. Delete the temporary file after processing.

## Sharing Link Lifecycle (180-Day Expiration)

When a sharing event is processed, each anonymous/org-wide sharing permission is enrolled in `sharing_link_lifecycle`. Links with a Microsoft-set `expirationDateTime` are marked `ms_managed` and exempt from countdown notifications.

**Milestone schedule for active links:**

| Days | Action | Remaining |
|-|-|-|
| 120 | First countdown email to file owner | 60 |
| 150 | Second countdown email | 30 |
| 165 | Third countdown email | 15 |
| 173 | Urgent reminder email | 7 |
| 178 | Final warning email | 2 |
| 180 | Remove link via Graph API + confirmation email | 0 |

The lifecycle processor runs daily in the lifecycle-cron container.

## Architecture

The system runs as Docker Compose services on an Ubuntu server, with a future migration path to Kubernetes.

### Containers

- **lifecycle-cron**: Runs the concurrent loops — (1) audit log poller that queries the Graph API every 15 minutes for new sharing events (plus a periodic look-back reconciliation pass, `AUDIT_POLL_LOOKBACK_HOURS`/`AUDIT_POLL_RECONCILE_INTERVAL_HOURS`, that catches audit records surfacing hours after the activity), (2) lifecycle processor that checks sharing link expiry milestones daily and removes expired links, (3) site policy scanner: daily visibility + SharePoint sharing scan and a weekly OneDrive sharing sweep, both using the paged PnP inventory, (4) folder rescan weekly, and (5+) the CWS PowerShell loops (verification sweep, drift remediation, bulk remediation, exception-group sync, allowlist actions, failure diagnostics).
- **worker**: Python service that pulls jobs from Redis (up to 5 concurrently, via BLMOVE to a processing list so in-flight jobs survive a crash and are reclaimed at startup), orchestrates the full processing pipeline (metadata pre-screen, download, text extraction, AI analysis, lifecycle enrollment, notification, cleanup). AI analysis fails CLOSED: provider failures and unparseable responses are retried and then mark the event failed with an analyst notification — never recorded as a clean verdict.
- **dashboard**: React + FastAPI web UI for analysts to review events, verdicts, and statistics.
- **redis**: Job queue, deduplication cache, and rate limiting state.
- **postgres**: Event records, AI verdicts, analyst dispositions, sharing link lifecycle, audit poll state.
- **transcriber**: Whisper-based transcription service for audio/video content analysis.
- **watchdog**: Monitors lifecycle-cron loop heartbeats (auto-restarts the container on dead loops) plus system-level alert-only checks: worker heartbeat, jobs-queue depth, dead-letter queues, failed scans/events, inspection-queue age. Alerts via Teams webhook and the `watchdog_alerts` table.
- **db-backup**: Daily `pg_dump` to `./backups/` (gitignored) with 14-day retention.

### Key Technology Choices

- **Language**: Python 3.12+
- **Event ingestion**: Direct polling of Microsoft Graph Audit Log Query API (beta)
- **Queue**: Redis (using Redis lists for job queuing)
- **Database**: PostgreSQL 16
- **File storage**: tmpfs (RAM-backed mount in the worker container; files never touch persistent disk)
- **Text extraction**: PyMuPDF (PDFs), python-docx (Word), openpyxl (Excel), python-pptx (PowerPoint), Tesseract OCR (scanned documents)
- **Image processing**: Pillow (resizing/compression)
- **AI providers**: Anthropic Claude API, OpenAI API, Google Gemini API (abstracted behind a common interface)
- **Notifications**: Email (SMTP) for analyst alerts and lifecycle countdown emails; Jira ticket creation (Phase 2)
- **SharePoint admin**: Office365-REST-Python-Client (CSOM) for per-site sharing capability management (Graph API does not support this)

## Sensitivity Detection (Category-Based)

The AI returns detected sensitivity categories rather than a numeric rating. Categories are organized into tiers:

- **Tier 1 (urgent)**: `pii_government_id`, `pii_financial`, `ferpa`, `hipaa`, `security_credentials`
- **Tier 2 (normal)**: `hr_personnel`, `legal_confidential`, `pii_contact`
- **Tier 3 (no escalation)**: `coursework`, `casual_personal`, `none`

Escalation is deterministic: any Tier 1 or Tier 2 category triggers analyst notification. There is no configurable threshold.

## Detailed Planning Documents

The following documents in the `docs/` directory contain detailed specifications for each component.

| Document | Description |
|-|-|
| [docs/01-architecture-overview.md](docs/01-architecture-overview.md) | Full system architecture, data flow diagrams, container layout, technology rationale |
| [docs/02-event-ingestion-service.md](docs/02-event-ingestion-service.md) | Event ingestion via audit log poller, Graph API query flow, record-to-job mapping |
| [docs/03-file-processing-pipeline.md](docs/03-file-processing-pipeline.md) | Master orchestration logic for the worker: metadata pre-screen, download, classification, routing |
| [docs/04-text-extraction-module.md](docs/04-text-extraction-module.md) | Text extraction strategies for every supported file type, sampling logic, OCR fallback |
| [docs/05-image-preprocessing-module.md](docs/05-image-preprocessing-module.md) | Image resizing, compression, scanned PDF page rendering, multimodal preparation |
| [docs/06-ai-provider-abstraction.md](docs/06-ai-provider-abstraction.md) | Provider interface, prompt management, structured output parsing, cost tracking, provider switching |
| [docs/07-database-schema.md](docs/07-database-schema.md) | PostgreSQL tables, indexes, audit logging, migration strategy |
| [docs/08-notification-service.md](docs/08-notification-service.md) | Email alerting, Jira integration, notification interface design |
| [docs/09-configuration-deployment.md](docs/09-configuration-deployment.md) | Docker Compose setup, environment variables, secrets, health checks, operations |
| [docs/10-testing-calibration.md](docs/10-testing-calibration.md) | Test strategy, AI provider benchmarking, sample sensitive file creation, validation methodology |

## Project Structure

```
share-sentinel/
├── CLAUDE.md                          # This file - primary project reference
├── docker-compose.yml                 # Container orchestration
├── .env.example                       # Environment variable template
├── docs/                              # Planning documents (detailed specs)
├── services/
│   ├── lifecycle-cron/                # Audit log poller + lifecycle + site policy + folder rescan
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py               # Entry point (up to 4 concurrent loops)
│   │       ├── config.py             # LifecycleConfig
│   │       ├── audit_poller.py       # Graph Audit Log Query API poller
│   │       ├── processor.py          # Lifecycle milestone processor
│   │       ├── notifier.py           # Countdown email notifications
│   │       ├── graph_api.py          # Graph API auth + helpers (groups, visibility)
│   │       ├── site_policy_scanner.py # Dual site policy scanner (visibility + sharing)
│   │       ├── allowlist_enforcer.py  # SharePoint CSOM sharing capability management
│   │       └── folder_rescan.py      # Weekly folder rescan for new/modified files
│   ├── worker/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── app/
│   │   │   ├── main.py              # Worker entry point (queue consumer loop)
│   │   │   ├── config.py            # Configuration loading
│   │   │   ├── pipeline/
│   │   │   │   ├── orchestrator.py  # Master pipeline logic
│   │   │   │   ├── metadata.py      # Graph API metadata pre-screen
│   │   │   │   ├── downloader.py    # Graph API file download
│   │   │   │   ├── classifier.py    # File type classification and routing
│   │   │   │   ├── hasher.py        # File hash computation and dedup check
│   │   │   │   ├── second_look.py   # Cross-provider AI verification
│   │   │   │   └── cleanup.py       # Temp file deletion
│   │   │   ├── extraction/          # Text extractors (PDF, DOCX, XLSX, etc.)
│   │   │   ├── ai/                  # AI provider abstraction
│   │   │   ├── notifications/       # Email + Jira notifiers
│   │   │   ├── database/
│   │   │   │   ├── connection.py    # PostgreSQL connection management
│   │   │   │   ├── repositories.py  # Data access methods
│   │   │   │   └── migrations/      # Schema migrations (001-022)
│   │   │   ├── graph_api/           # Graph API auth + client + sharing
│   │   │   ├── lifecycle/
│   │   │   │   └── enrollment.py    # Sharing link lifecycle enrollment
│   │   │   └── remediation/         # Sharing link removal poller
│   │   └── tests/
│   └── dashboard/
│       ├── Dockerfile
│       ├── app/                      # FastAPI backend
│       │   ├── api/
│       │   │   └── allowlist.py     # Site policy allow list + scan API endpoints
│       │   └── graph_helper.py      # Graph API helpers (site/group search, details)
│       └── frontend/                 # React (Vite) frontend
│           └── src/pages/AllowList.tsx  # Tabbed UI (Sharing, Visibility, Events)
├── config/
│   ├── prompt_templates/
│   │   └── sensitivity_analysis_v2.txt  # Category-based AI prompt template
│   ├── file_types.yml               # File type classification config
│   └── notification_templates/
│       ├── analyst_alert.html       # Email template for analyst notifications
│       └── lifecycle_countdown.html # Email template for lifecycle countdown
└── scripts/
    ├── init_db.sql                  # Database initialization
    ├── backfill_lifecycle_enrollment.py  # Backfill lifecycle rows for older events
    └── various utility scripts
```

## Critical Design Decisions

### Events are ingested via direct audit log polling
The system queries the Microsoft Graph Audit Log Query API directly every 15 minutes. Polling avoids the complexity of webhook infrastructure and provides a direct, reliable event pipeline.

### Files are ALWAYS text-extracted first
For every file type that supports text extraction (PDF, DOCX, XLSX, PPTX, CSV, TXT), always extract text before sending to the AI. Send extracted text as a text-based prompt (cheaper, faster, often more accurate). Only fall back to multimodal (image-based) analysis when text extraction fails (scanned documents, actual images). This minimizes API costs significantly.

### Multimodal is the fallback, not the default
The multimodal path (sending images to the AI) is reserved for: actual image files (PNG, JPG, TIFF, etc.), scanned PDFs where text extraction and OCR both fail, and PDF pages that contain primarily visual content. Everything else goes through text extraction.

### Folder shares are enumerated and analyzed, then flagged
When a folder is shared with anonymous or org-wide access, the worker enumerates its contents via Graph API and analyzes each child file individually (download, text extraction, AI analysis). The folder is also flagged for analyst review because its risk grows over time as new files inherit the sharing scope.

### Delegated content types use browser screenshots
Loop components, OneNote notebooks, and Whiteboards cannot be fetched via the application-level Graph API. These items are parked as `pending_manual_inspection` by the worker and processed through the dashboard's Inspection Queue. Processing uses a headless Playwright browser with saved authentication cookies (obtained via an interactive browser session streamed to the dashboard) to screenshot the sharing URL, then runs multimodal AI analysis on the captured image.

### Temporary files never touch persistent disk
All downloaded files are stored on a tmpfs mount (RAM-backed filesystem) in the worker container. This ensures files are automatically cleaned up on container restart and never persist to disk. The worker also explicitly deletes files after processing.

### AI provider is swappable via configuration
The system uses an abstract provider interface. Switching between Anthropic, OpenAI, and Google Gemini requires only a configuration change, not a code change. All providers implement the same interface and return the same structured output format.

### Sharing links are automatically removed after 180 days
All anonymous and org-wide sharing links are enrolled in a lifecycle tracker. The system sends countdown notifications to file owners at 120, 150, 165, 173, and 178 days, then removes the link via the Graph API at 180 days. Links with Microsoft-managed expiration dates are exempt.

### Filename/path analysis is the fallback for unprocessable files
Files that are too large to download, are excluded types (video, audio, binaries), or fail all extraction methods still get analyzed. The AI receives the filename, file path, file size, and sharing metadata and provides a sensitivity assessment based solely on that information.

### Site-level policies are enforced via dual allow lists
The system enforces two distinct site-level policies, each with its own allow list:

1. **Visibility Policy**: All M365 Unified groups are expected to be Private. Groups found to be Public that are not on the visibility allow list (`site_visibility_allowlist`) are automatically set to Private via the Graph API (`PATCH /groups/{id}`).

2. **Sharing Capability Policy**: All SharePoint sites are expected to block anonymous sharing links. Sites found with `SharingCapability = ExternalUserAndGuestSharing` that are not on the sharing allow list (`site_allowlist`) are downgraded to `ExternalUserSharingOnly` via the SharePoint CSOM tenant admin API. **Site inventory comes from PnP PowerShell** (`scripts/Get-SitePolicyInventory.ps1`, which pages correctly at tenant scale — the raw CSOM `getSitePropertiesFromSharePointByFilters` REST call returns only its first ~1,400 rows with no continuation cursor and must never be reintroduced). The **daily scan** covers SharePoint sites (~90K); a separate **weekly OneDrive sweep** (`SITE_POLICY_ONEDRIVE_INTERVAL_HOURS`, gated by `SITE_POLICY_INCLUDE_ONEDRIVE`) covers personal sites (~145K), with event-driven remediation covering the gap between sweeps. If a site's `SharingCapability` comes back uninitialized from the inventory, the scanner resolves the authoritative value with a per-site call rather than treating it as compliant. A scan whose enumeration returns fewer than `SITE_POLICY_SCAN_FLOOR_PCT` (default 50%) of the recent peak for its scan type is marked **failed**, never completed — a truncated inventory must not read as a clean pass. Enforcement never *loosens* a site: `disable_sharing` only acts when the current capability actually allows anonymous links (a site already at `Disabled`/`ExistingExternalUserSharingOnly` is left untouched).

Both policies are enforced by a daily scan in the lifecycle-cron container. Dashboard administrators can also add/remove sites from allow lists, which triggers immediate enforcement actions via Redis. The Graph API is used for group visibility changes, while the Office365-REST-Python-Client (CSOM) library is required for SharePoint site sharing capability changes because the Graph API does not support per-site `SharingCapability` management.

### Event-driven remediation on share-link creation
Because we already receive an audit event the moment a sharing link is created, the worker checks each org-wide/anonymous link against the relevant allow list and (when `EVENT_REMEDIATION_ENABLED=true`) files an immediate single-site remediation rather than waiting for the next sweep. This is implemented in `services/worker/app/pipeline/cws_drift_detector.py` and reuses the existing lifecycle-cron action queues:

- **`AnonymousLinkCreated`** on a site/OneDrive not in `site_allowlist` → pushes `disable_sharing` to `sharesentinel:site_policy_action` (CSOM `SharingCapability` downgrade). Unlike the scheduled scan, this covers **OneDrive personal sites** as well as SharePoint sites. Requires `SITE_POLICY_ENABLED=true` (its loop drains the queue).
- **`CompanyLinkCreated`** on a site not in `site_company_wide_sharing_allowlist` and whose actor is not in the CWS exception group → records a detect-only drift row (daily-drain safety net) **and** pushes `cws_disallow_site` to `sharesentinel:company_wide_sharing_action` (`Invoke-CwsAllowlistAction.ps1`, idempotent). Requires the CWS loops enabled.

Both pushes are deduped per site with a short-TTL Redis key so a burst of events doesn't flood the queues. The CWS **verification sweep** (full-tenant enumeration) is slow, so it runs **weekly** (`CWS_VERIFICATION_INTERVAL_HOURS=168`); the **drift remediation** drain stays **daily** to apply anything recorded between sweeps. The CWS exception group membership is mirrored into the Redis set `sharesentinel:cws_exception_upns` by the lifecycle-cron exception-group sync loop (every tick), which is what the worker's drift detector reads — the exception-group sync loop must be enabled for event-driven CWS exemptions to work (the worker logs an ERROR at startup if the set is missing while EVENT_REMEDIATION_ENABLED=true). Exception-group membership is **direct members only by design** (2026-07-05 decision): nesting a group inside the exception group does NOT confer exemption. PowerShell exemption matching uses forward-encoded personal-site segments (`ConvertTo-CwsPersonalSegment` in `scripts/lib/CwsSiteUrl.ps1`) — never reverse-decode a OneDrive URL to a UPN for policy decisions; that direction is ambiguous for dotted UPNs.

### Event-driven remediation on site/group/user creation
With `AUDIT_POLL_PROVISIONING_ENABLED=true`, the audit log poller also queries `SiteCollectionCreated` (record type `sharePoint`) and `Add group.` / `Add user.` (record type `azureActiveDirectory`) and routes those records to `services/lifecycle-cron/app/provisioning_enforcer.py` instead of the worker queue:

- **`SiteCollectionCreated`** (new SharePoint site or newly provisioned OneDrive) → pushes `disable_sharing` (unless on `site_allowlist`; needs `SITE_POLICY_ENABLED=true`) and `cws_disallow_site` (unless on `site_company_wide_sharing_allowlist`, or a OneDrive whose owner is in the CWS exception group). New sites are born with `DisableCompanyWideSharingLinks = NotSpecified` (CWS allowed), so this closes the window between creation and the next sweep.
- **`Add group.`** → looks the group up via Graph; a Unified group created **Public** and not on `site_visibility_allowlist` gets a `set_private` action (whose consumer also cascades the sharing + CWS policies onto the group's site).
- **`Add user.`** → logged only. A user's OneDrive does not exist at account creation; it is provisioned on first OneDrive access, which emits the `SiteCollectionCreated` event handled above.

This requires the **full** `AuditLogsQuery.Read.All` application permission — the SharePoint-scoped variant (`AuditLogsQuery-SharePoint.Read.All`) cannot read `azureActiveDirectory` records. If the combined query is rejected, the poller falls back to sharing-only filters for that cycle and logs an error, so link-event ingestion is never broken by a missing permission.

## Important Constraints

- **Volume**: Less than 100 sharing events per day on average.
- **Latency tolerance**: High. Even multi-day processing delays are acceptable.
- **File size download limit**: 50MB. Files larger than this get filename/path analysis only.
- **Text content limit for AI**: 100KB of extracted text (~25K tokens). Content exceeding this is sampled.
- **Image size limit for multimodal**: Resize to longest edge 1600px, compress to JPEG quality 85, target under 1MB.
- **Sensitivity escalation**: Any Tier 1 or Tier 2 category triggers analyst notification. No configurable threshold.
- **Sharing link expiration**: 180 days from link creation, with milestone notifications at 120, 150, 165, 173, and 178 days.
- **Excluded file types**: Video (.mp4, .mov, .avi, .mkv, .wmv), Audio (.mp3, .wav, .m4a, .aac, .flac), Binaries (.exe, .dll, .bin), and other non-document types defined in config/file_types.yml.
- **Site policy scan interval**: Configurable (default 24 hours). Scans all M365 groups for visibility violations and all SharePoint sites for sharing capability violations.
- **Site policy enforcement**: Immediate when allow list entries are added/removed via dashboard; daily for scheduled full scans.

## Development Workflow

### Execute Code Inside Docker Containers Only
All code execution, testing, and debugging MUST happen inside the Docker containers — never on the host machine directly. The services (worker, lifecycle-cron, dashboard) depend on Redis, PostgreSQL, and inter-service networking that are only available within the Docker Compose environment.

- **Run scripts**: Use `docker exec` to run commands inside the appropriate container (e.g., `docker exec sharesentinel-worker python -m scripts.backfill_lifecycle_enrollment --dry-run`).
- **Enqueue test jobs**: Use `docker exec sharesentinel-redis redis-cli RPUSH sharesentinel:jobs '{...}'` to push jobs onto the Redis queue.
- **View logs**: Use `docker compose logs -f <service>` to follow container logs.
- **Database queries**: Use `docker exec sharesentinel-postgres psql -U sharesentinel -d sharesentinel -c "SELECT ..."`.
- **Rebuild after code changes**: Use `docker compose up --build -d <service>` to rebuild and restart a specific service.
- **Never install Python dependencies on the host** for running service code. The containers have their own isolated dependency sets.

## Environment and Authentication

- **Microsoft Graph API**: Azure AD app registration with application permissions for `Files.Read.All`, `Sites.Read.All`, `Sites.FullControl.All` (for link removal), `AuditLogsQuery.Read.All` (for audit log polling), and `Group.ReadWrite.All` (for site visibility policy enforcement). Uses client credentials flow (client_id + client_secret + tenant_id) with optional certificate auth.
- **SharePoint Admin CSOM**: The same Azure AD app registration authenticates to the SharePoint tenant admin site (`https://yourtenant-admin.sharepoint.com`) via certificate or client secret to manage per-site `SharingCapability` settings. Requires the `SHAREPOINT_ADMIN_URL` environment variable.
- **AI APIs**: API keys for each provider stored as environment variables or Docker secrets.
- **SMTP**: Configured for analyst email notifications and lifecycle countdown emails.
- **PostgreSQL**: Connection string via environment variable.
- **Redis**: Connection string via environment variable.
