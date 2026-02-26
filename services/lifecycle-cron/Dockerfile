FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Non-root user
RUN useradd -m -r appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
