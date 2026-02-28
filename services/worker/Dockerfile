FROM python:3.12-slim

WORKDIR /app

# System deps: OCR engine, file-type detection, PDF utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libmagic1 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install shared package
COPY services/shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared

COPY services/worker/app/ ./app/

# tmpfs mount point (actual tmpfs configured in docker-compose)
RUN mkdir -p /tmp/sharesentinel

# Non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app /tmp/sharesentinel
USER appuser

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import os, redis; r = redis.from_url(os.environ.get('REDIS_URL', 'redis://redis:6379/0')); r.ping()"

CMD ["python", "-m", "app.main"]
