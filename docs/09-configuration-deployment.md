# 09 - Configuration, Deployment, and Operations

## Purpose

This document specifies the Docker Compose setup, Dockerfiles, environment variables, health checks, monitoring, and operational considerations for running ShareSentinel.

## Docker Compose

```yaml
version: '3.8'

services:
  webhook-listener:
    build:
      context: ./services/webhook-listener
      dockerfile: Dockerfile
    container_name: sharesentinel-webhook
    ports:
      - "${WEBHOOK_HOST_PORT:-8000}:8000"
    environment:
      - WEBHOOK_PORT=8000
      - WEBHOOK_AUTH_SECRET=${WEBHOOK_AUTH_SECRET}
      - REDIS_URL=redis://redis:6379/0
      - DEDUP_TTL_SECONDS=${DEDUP_TTL_SECONDS:-86400}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - sharesentinel
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

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
      
      # Processing
      - MAX_FILE_SIZE_BYTES=${MAX_FILE_SIZE_BYTES:-52428800}
      - TEXT_CONTENT_LIMIT=${TEXT_CONTENT_LIMIT:-100000}
      - SENSITIVITY_THRESHOLD=${SENSITIVITY_THRESHOLD:-4}
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

### Webhook Listener Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install curl for health check
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run as non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

**requirements.txt:**
```
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
redis[hiredis]>=5.0.0
pydantic>=2.5.0
python-json-logger>=2.0.0
```

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

# Create tmpfs mount point
RUN mkdir -p /tmp/sharesentinel

# Run as non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app /tmp/sharesentinel
USER appuser

CMD ["python", "-m", "app.main"]
```

**requirements.txt:**
```
# Queue and database
redis[hiredis]>=5.0.0
asyncpg>=0.29.0
psycopg2-binary>=2.9.9

# Microsoft Graph API
httpx>=0.25.0
msal>=1.25.0

# Text extraction
PyMuPDF>=1.23.0
python-docx>=0.8.11
openpyxl>=3.1.0
python-pptx>=0.6.21

# OCR
pytesseract>=0.3.10

# Image processing
Pillow>=10.0.0
pillow-heif>=0.13.0

# AI providers
anthropic>=0.37.0
openai>=1.6.0
google-generativeai>=0.3.0

# Utilities
pydantic>=2.5.0
python-json-logger>=2.0.0
jinja2>=3.1.0
pyyaml>=6.0.0
python-magic>=0.4.27
```

## Environment Variables Template (.env.example)

```bash
# ============================================================
# ShareSentinel Configuration
# Copy this file to .env and fill in your values
# ============================================================

# --- Webhook Listener ---
WEBHOOK_HOST_PORT=8000
WEBHOOK_AUTH_SECRET=your-shared-secret-here
DEDUP_TTL_SECONDS=86400

# --- PostgreSQL ---
POSTGRES_DB=sharesentinel
POSTGRES_USER=sharesentinel
POSTGRES_PASSWORD=CHANGE_ME_strong_password_here

# --- Microsoft Graph API (Azure AD App Registration) ---
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret

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

# --- Processing ---
MAX_FILE_SIZE_BYTES=52428800
TEXT_CONTENT_LIMIT=100000
SENSITIVITY_THRESHOLD=4
HASH_REUSE_DAYS=30

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

# Jira (Phase 2)
JIRA_URL=https://yourorg.atlassian.net
JIRA_EMAIL=sharesentinel@yourorg.com
JIRA_API_TOKEN=your-jira-api-token
JIRA_PROJECT_KEY=SECOPS
JIRA_ISSUE_TYPE=Task

# --- Logging ---
LOG_LEVEL=INFO
```

## Health Checks

### Webhook Listener

`GET /health` returns:
```json
{
  "status": "healthy",
  "redis_connected": true,
  "uptime_seconds": 3600,
  "version": "1.0.0"
}
```

### Worker

The worker does not expose HTTP endpoints. Health is monitored via:

1. **Docker healthcheck**: A script that checks if the worker process is running and if the last successful job processing was within the expected timeframe.

```dockerfile
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import redis; r = redis.from_url('redis://redis:6379/0'); r.ping()"
```

2. **Heartbeat**: The worker writes a heartbeat timestamp to Redis every 60 seconds: `SET sharesentinel:worker:heartbeat <timestamp>`. Monitoring can check this key to verify the worker is alive and processing.

3. **Dead letter monitoring**: Check the database for events stuck in "processing" status for more than 30 minutes. These indicate a worker crash or hang.

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

Log output goes to stdout/stderr (standard for containerized applications). Docker collects logs from container stdout. For production, configure a log driver (e.g., `json-file` with rotation, or forward to a centralized logging system).

Docker Compose logging config:
```yaml
services:
  webhook-listener:
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
```

## Startup Sequence

1. **PostgreSQL** starts first and runs `init_db.sql` to create the schema.
2. **Redis** starts and passes its health check.
3. **Webhook listener** starts after Redis is healthy. Tests Redis connection on startup.
4. **Worker** starts after both Redis and PostgreSQL are healthy. Runs database migrations on startup. Tests connections to Redis, PostgreSQL, and Graph API (token acquisition). Starts the main processing loop.

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
|----------------|------------|
| `webhook-listener` service | Deployment + Service (ClusterIP or LoadBalancer) |
| `worker` service | Deployment (no Service needed; it's a queue consumer) |
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

**Redis**: Redis persistence (AOF) is enabled in the compose file. Data can be recovered from the AOF file on restart. If Redis data is lost, the only impact is that the dedup cache is cleared (some events might be re-processed, which is harmless) and any in-flight jobs in the queue are lost (Splunk can be configured to retry, or they'll be caught on the next alert).

**Configuration**: The `config/` directory and `.env` file should be version-controlled (except `.env`, which should be backed up separately).

## Monitoring Suggestions

For the MVP, monitoring is basic (log review, health check endpoints). For production maturity, consider:

- **Uptime monitoring**: External check that the `/health` endpoint returns 200.
- **Queue depth monitoring**: Check `LLEN sharesentinel:jobs` in Redis. If the queue grows beyond a threshold, something is wrong with the worker.
- **Processing lag**: Track the time between `received_at` and `processing_completed_at` in the database. If lag grows, the worker may be struggling.
- **Failure rate**: Track the ratio of `failed` to `completed` events in the database.
- **AI API availability**: Track API call success rates by provider.
- **Disk/RAM usage**: Monitor the tmpfs mount usage to ensure it doesn't fill up.
