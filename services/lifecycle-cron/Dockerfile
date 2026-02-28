FROM python:3.12-slim

WORKDIR /app

COPY services/lifecycle-cron/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install shared package
COPY services/shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared && rm -rf /tmp/shared

COPY services/lifecycle-cron/app/ ./app/

# Non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
