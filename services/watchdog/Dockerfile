FROM python:3.12-slim

WORKDIR /app

COPY services/watchdog/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/watchdog/app/ ./app/

CMD ["python", "-m", "app.main"]
