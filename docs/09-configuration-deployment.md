# 09 - Configuration, Deployment, and Operations

## Purpose

This document specifies the Docker Compose setup, Dockerfiles, environment variables, health checks, monitoring, and operational considerations for running ShareSentinel.

## Important

`docker-compose.yml` in the repository root is the authoritative runtime configuration.
Use this document for architecture/operations guidance, but prefer the compose file for
exact service definitions, environment variable wiring, and security settings.

## Docker Compose

```yaml
version: '3.8'

services:
  worker:
    build:
      context: ./services/worker
      dockerfile: Dockerfile
    container_name: sharesentinel-worker
    tmpfs:
      - /tmp/sharesentinel:size=256M,mode=1777
    environment:
      # Redis
      - REDIS_URL=redis://redis:6379/0

      # Database
      - DATABASE_URL=postgresql://${POSTGRES_USER:-sharesentinel}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-sharesentinel}

      # Microsoft Graph API
      - AZURE_TENANT_ID=${AZURE_TENANT_ID}
      - AZURE_CLIENT_ID=${AZURE_CLIENT_ID}
      - AZURE_CLIENT_SECRET=${AZURE_CLIENT_SECRET}
      - AZURE_CERTIFICATE=/app/certs/graph-api-cert.pfx
      - AZURE_CERTIFICATE_PASS=${AZURE_CERTIFICATE_PASS}

      # AI Provider
      - AI_PROVIDER=${AI_PROVIDER:-anthropic}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-claude-sonnet-4-5-20250929}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_MODEL=${OPENAI_MODEL:-gpt-4o}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.0-flash}
      - AI_TEMPERATURE=${AI_TEMPERATURE:-0}
      - AI_MAX_TOKENS=${AI_MAX_TOKENS:-1024}

      # Second-Look AI Review
      - SECOND_LOOK_ENABLED=${SECOND_LOOK_ENABLED:-false}
      - SECOND_LOOK_PROVIDER=${SECOND_LOOK_PROVIDER:-gemini}
      - SECOND_LOOK_MODEL=${SECOND_LOOK_MODEL:-gemini-3.1-pro-preview}

      # Processing
      - MAX_CONCURRENT_JOBS=${MAX_CONCURRENT_JOBS:-5}
      - MAX_FILE_SIZE_BYTES=${MAX_FILE_SIZE_BYTES:-52428800}
      - TEXT_CONTENT_LIMIT=${TEXT_CONTENT_LIMIT:-100000}
      - HASH_REUSE_DAYS=${HASH_REUSE_DAYS:-30}

      # Notifications
      - NOTIFICATION_CHANNELS=${NOTIFICATION_CHANNELS:-email}
      - NOTIFY_ON_FOLDER_SHARE=${NOTIFY_ON_FOLDER_SHARE:-true}
      - NOTIFY_ON_FAILURE=${NOTIFY_ON_FAILURE:-true}
      - SMTP_HOST=${SMTP_HOST}
      - SMTP_PORT=${SMTP_PORT:-587}
      - SMTP_USER=${SMTP_USER}
      - SMTP_PASSWORD=${SMTP_PASSWORD}
      - SMTP_USE_TLS=${SMTP_USE_TLS:-true}
      - EMAIL_FROM=${EMAIL_FROM}
      - EMAIL_TO=${EMAIL_TO}

      # Security / remediation
      - SECURITY_EMAIL=${SECURITY_EMAIL:-security@yourorg.com}

      # Jira (Phase 2)
      - JIRA_URL=${JIRA_URL}
      - JIRA_EMAIL=${JIRA_EMAIL}
      - JIRA_API_TOKEN=${JIRA_API_TOKEN}
      - JIRA_PROJECT_KEY=${JIRA_PROJECT_KEY}
      - JIRA_ISSUE_TYPE=${JIRA_ISSUE_TYPE:-Task}

      # Logging
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./config:/app/config:ro
      - ${AZURE_CERTIFICATE:-./graph-api-cert.pfx}:/app/certs/graph-api-cert.pfx:ro
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - sharesentinel

  dashboard:
    build:
      context: ./services/dashboard
      dockerfile: Dockerfile
    container_name: sharesentinel-dashboard
    ports:
      - "${DASHBOARD_HOST_PORT:-8080}:8080"
    environment:
      - DATABASE_URL=postgresql://${POSTGRES_USER:-sharesentinel}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-sharesentinel}
      - ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-http://localhost:5173,http://localhost:8080,http://127.0.0.1:8080}
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - sharesentinel

  lifecycle-cron:
    build:
      context: ./services/lifecycle-cron
      dockerfile: Dockerfile
    container_name: sharesentinel-lifecycle-cron
    environment:
      # Database
      - DATABASE_URL=postgresql://${POSTGRES_USER:-sharesentinel}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-sharesentinel}

      # Redis (audit log poller)
      - REDIS_URL=redis://redis:6379/0

      # Microsoft Graph API
      - AZURE_TENANT_ID=${AZURE_TENANT_ID}
      - AZURE_CLIENT_ID=${AZURE_CLIENT_ID}
      - AZURE_CLIENT_SECRET=${AZURE_CLIENT_SECRET}
      - AZURE_CERTIFICATE=/app/certs/graph-api-cert.pfx
      - AZURE_CERTIFICATE_PASS=${AZURE_CERTIFICATE_PASS}

      # Notifications
      - SMTP_HOST=${SMTP_HOST}
      - SMTP_PORT=${SMTP_PORT:-587}
      - SMTP_USER=${SMTP_USER}
      - SMTP_PASSWORD=${SMTP_PASSWORD}
      - SMTP_USE_TLS=${SMTP_USE_TLS:-true}
      - EMAIL_FROM=${EMAIL_FROM}
      - SECURITY_EMAIL=${SECURITY_EMAIL:-security@yourorg.com}

      # Lifecycle settings
      - LIFECYCLE_CHECK_INTERVAL_HOURS=${LIFECYCLE_CHECK_INTERVAL_HOURS:-24}
      - LIFECYCLE_MAX_DAYS=${LIFECYCLE_MAX_DAYS:-180}

      # Audit log polling
      - AUDIT_POLL_ENABLED=${AUDIT_POLL_ENABLED:-true}
      - AUDIT_POLL_INTERVAL_MINUTES=${AUDIT_POLL_INTERVAL_MINUTES:-15}

      # Site policy enforcement (dual: visibility + sharing)
      - SITE_POLICY_ENABLED=${SITE_POLICY_ENABLED:-false}
      - SITE_POLICY_INTERVAL_HOURS=${SITE_POLICY_INTERVAL_HOURS:-24}
      - SITE_POLICY_ENABLED_SHARING_CAPABILITY=${SITE_POLICY_ENABLED_SHARING_CAPABILITY:-ExternalUserAndGuestSharing}
      - SITE_POLICY_DISABLED_SHARING_CAPABILITY=${SITE_POLICY_DISABLED_SHARING_CAPABILITY:-ExternalUserSharingOnly}
      - SHAREPOINT_ADMIN_URL=${SHAREPOINT_ADMIN_URL}

      # Logging
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./config:/app/config:ro
      - ${AZURE_CERTIFICATE:-./graph-api-cert.pfx}:/app/certs/graph-api-cert.pfx:ro
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - sharesentinel

  redis:
    image: redis:7-alpine
    container_name: sharesentinel-redis
    command: redis-server --appendonly yes --maxmemory 128mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    restart: unless-stopped
    networks:
      - sharesentinel
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 5s

  postgres:
    image: postgres:16-alpine
    container_name: sharesentinel-postgres
    environment:
      - POSTGRES_DB=${POSTGRES_DB:-sharesentinel}
      - POSTGRES_USER=${POSTGRES_USER:-sharesentinel}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init_db.sql:/docker-entrypoint-initdb.d/init_db.sql:ro
    restart: unless-stopped
    networks:
      - sharesentinel
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-sharesentinel} -d ${POSTGRES_DB:-sharesentinel}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

volumes:
  redis_data:
  postgres_data:

networks:
  sharesentinel:
    driver: bridge
```

## Dockerfiles

### Worker Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
# tesseract-ocr: OCR engine for scanned documents
# libmagic1: file type detection
# poppler-utils: PDF utilities (used by some PDF libraries)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libmagic1 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# Create tmpfs mount point
RUN mkdir -p /tmp/sharesentinel

# Run as non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app /tmp/sharesentinel
USER appuser

CMD ["python", "-m", "app.main"]
```

### Lifecycle Cron Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m -r appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
```

## Environment Variables Template (.env.example)

```bash
# ============================================================
# ShareSentinel Configuration
# Copy this file to .env and fill in your values
# ============================================================

# --- PostgreSQL ---
POSTGRES_DB=sharesentinel
POSTGRES_USER=sharesentinel
POSTGRES_PASSWORD=CHANGE_ME_strong_password_here

# --- Microsoft Graph API (Azure AD App Registration) ---
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret
AZURE_CERTIFICATE=./graph-api-cert.pfx
AZURE_CERTIFICATE_PASS=your-cert-password

# --- AI Provider ---
# Options: anthropic, openai, gemini
AI_PROVIDER=anthropic

# Anthropic
ANTHROPIC_API_KEY=sk-ant-your-key-here
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929

# OpenAI
OPENAI_API_KEY=sk-your-key-here
OPENAI_MODEL=gpt-4o

# Google Gemini
GEMINI_API_KEY=your-key-here
GEMINI_MODEL=gemini-2.0-flash

# AI Settings
AI_TEMPERATURE=0
AI_MAX_TOKENS=1024

# Second-Look AI Review (cross-provider verification)
SECOND_LOOK_ENABLED=false
SECOND_LOOK_PROVIDER=gemini
SECOND_LOOK_MODEL=gemini-3.1-pro-preview

# --- Processing ---
MAX_CONCURRENT_JOBS=5
MAX_FILE_SIZE_BYTES=52428800
TEXT_CONTENT_LIMIT=100000
HASH_REUSE_DAYS=30

# --- Audit Log Polling ---
AUDIT_POLL_ENABLED=true
AUDIT_POLL_INTERVAL_MINUTES=15

# --- Lifecycle ---
LIFECYCLE_CHECK_INTERVAL_HOURS=24
LIFECYCLE_MAX_DAYS=180

# --- Site Policy Enforcement (dual: visibility + anonymous sharing) ---
# Enable daily enforcement of both site visibility and anonymous sharing policies
SITE_POLICY_ENABLED=false
SITE_POLICY_INTERVAL_HOURS=24
SITE_POLICY_ENABLED_SHARING_CAPABILITY=ExternalUserAndGuestSharing
SITE_POLICY_DISABLED_SHARING_CAPABILITY=ExternalUserSharingOnly
SHAREPOINT_ADMIN_URL=https://yourtenant-admin.sharepoint.com

# --- Notifications ---
# Comma-separated: email,jira
NOTIFICATION_CHANNELS=email
NOTIFY_ON_FOLDER_SHARE=true
NOTIFY_ON_FAILURE=true

# Email (SMTP)
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USER=sharesentinel@yourorg.com
SMTP_PASSWORD=CHANGE_ME
SMTP_USE_TLS=true
EMAIL_FROM=sharesentinel@yourorg.com
EMAIL_TO=analyst1@yourorg.com,analyst2@yourorg.com
SECURITY_EMAIL=security@yourorg.com

# Jira (Phase 2)
JIRA_URL=https://yourorg.atlassian.net
JIRA_EMAIL=sharesentinel@yourorg.com
JIRA_API_TOKEN=your-jira-api-token
JIRA_PROJECT_KEY=SECOPS
JIRA_ISSUE_TYPE=Task

# --- Dashboard ---
DASHBOARD_HOST_PORT=8080
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:8080

# --- Logging ---
LOG_LEVEL=INFO
```

## Health Checks

### Dashboard

`GET /api/health` returns:
```json
{
  "status": "healthy"
}
```

### Worker

The worker does not expose HTTP endpoints. Health is monitored via:

1. **Heartbeat**: The worker writes a heartbeat timestamp to Redis every 60 seconds: `SET sharesentinel:worker:heartbeat <timestamp>`. Monitoring can check this key to verify the worker is alive and processing.

2. **Dead letter monitoring**: Check the database for events stuck in "processing" status for more than 30 minutes. These indicate a worker crash or hang.

### Lifecycle Cron

The lifecycle-cron container does not expose HTTP endpoints. Health is monitored via:

1. **Audit poll state**: Query `SELECT * FROM audit_poll_state` to verify the last poll was recent and successful.
2. **Container logs**: `docker compose logs lifecycle-cron` shows poll and lifecycle cycle results.

## Logging Configuration

All services use structured JSON logging via `python-json-logger`:

```python
import logging
from pythonjsonlogger import jsonlogger

def setup_logging(service_name: str, level: str = "INFO"):
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper()))

    # Add service name to all log records
    old_factory = logging.getLogRecordFactory()
    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.service = service_name
        return record
    logging.setLogRecordFactory(record_factory)
```

Log output goes to stdout/stderr (standard for containerized applications). Docker collects logs from container stdout.

Docker Compose logging config (applied to all services):
```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "5"
```

## Startup Sequence

1. **PostgreSQL** starts first and runs `init_db.sql` to create the schema.
2. **Redis** starts and passes its health check.
3. **Worker** starts after both Redis and PostgreSQL are healthy. Runs database migrations on startup. Tests connections to Redis, PostgreSQL, and Graph API (token acquisition). Starts the main processing loop with concurrent job handling.
4. **Lifecycle Cron** starts after both Redis and PostgreSQL are healthy. Launches up to four concurrent tasks: lifecycle processor (daily), audit log poller (every 15 min), site policy scanner (daily, if `SITE_POLICY_ENABLED=true`), and folder rescan (weekly, if `FOLDER_RESCAN_ENABLED=true`).
5. **Dashboard** starts after PostgreSQL is healthy.

If any dependency is unavailable at startup, the dependent service will retry (Docker `restart: unless-stopped` handles this). The `depends_on` with `condition: service_healthy` ensures proper ordering on initial deployment.

## Secrets Management

For the MVP (Docker Compose on a single server), secrets are managed via the `.env` file. The `.env` file should:
- Have restricted file permissions (`chmod 600 .env`)
- Never be committed to version control (add to `.gitignore`)
- Be backed up securely

For future Kubernetes deployment, secrets should be migrated to Kubernetes Secrets objects and mounted as environment variables or files in the pods.

## Kubernetes Migration Notes

The Docker Compose setup maps to Kubernetes as follows:

| Docker Compose | Kubernetes |
|-|-|
| `worker` service | Deployment (no Service needed; it's a queue consumer) |
| `lifecycle-cron` service | Deployment (no Service needed; it polls on a schedule) |
| `dashboard` service | Deployment + Service (ClusterIP or LoadBalancer) |
| `redis` service | StatefulSet or managed Redis (e.g., Azure Cache for Redis) |
| `postgres` service | StatefulSet or managed PostgreSQL (e.g., Azure Database for PostgreSQL) |
| `.env` file | ConfigMap (non-sensitive) + Secret (sensitive) |
| `tmpfs` mount | `emptyDir` with `medium: Memory` in pod spec |
| Docker Compose network | Kubernetes namespace + Services for inter-pod communication |
| Docker volumes (redis_data, postgres_data) | PersistentVolumeClaims |
| Port exposure | Kubernetes Ingress or Service with type LoadBalancer |

Key changes for Kubernetes:
- The worker can be scaled to multiple replicas (Redis BLPOP handles distribution).
- Health checks become Kubernetes liveness and readiness probes.
- Log collection switches to a cluster-level solution (e.g., Fluentd, Loki).
- The `config/` directory is mounted via a ConfigMap.

## Backup and Recovery

**PostgreSQL**: The database volume should be backed up regularly. For the MVP, a daily `pg_dump` via cron is sufficient:
```bash
docker exec sharesentinel-postgres pg_dump -U sharesentinel sharesentinel > backup_$(date +%Y%m%d).sql
```

**Redis**: Redis persistence (AOF) is enabled in the compose file. Data can be recovered from the AOF file on restart. If Redis data is lost, the only impact is that the dedup cache is cleared (some events might be re-processed, which is harmless) and any in-flight jobs in the queue are lost (the audit poller will re-discover them on the next poll cycle with the overlap window).

**Configuration**: The `config/` directory and `.env` file should be version-controlled (except `.env`, which should be backed up separately).

## Monitoring Suggestions

For the MVP, monitoring is basic (log review, health check endpoints). For production maturity, consider:

- **Uptime monitoring**: External check that the dashboard `/api/health` endpoint returns 200.
- **Queue depth monitoring**: Check `LLEN sharesentinel:jobs` in Redis. If the queue grows beyond a threshold, something is wrong with the worker.
- **Audit poll monitoring**: Check `audit_poll_state.updated_at` is within the expected interval. If stale, the poller may have stopped.
- **Processing lag**: Track the time between `event_time` and `processing_completed_at` in the database. If lag grows, the worker may be struggling.
- **Failure rate**: Track the ratio of `failed` to `completed` events in the database.
- **Lifecycle coverage**: Check for events missing lifecycle enrollment rows (`events` rows without matching `sharing_link_lifecycle` entries).
- **AI API availability**: Track API call success rates by provider.
- **Disk/RAM usage**: Monitor the tmpfs mount usage to ensure it doesn't fill up.
- **Site policy scan health**: Check `site_policy_scans` for recent completed scans. If the latest scan is stale or has status `'failed'`, the site policy scanner may have stopped.
- **Site policy enforcement errors**: Track `site_policy_events` with `action = 'failed'` for persistent Graph API or CSOM failures.
- **Allow list drift**: Compare the count of Public M365 groups and anonymous-sharing-enabled sites against the allow list sizes to detect drift between scans.
