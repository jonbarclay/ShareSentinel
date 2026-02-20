"""FastAPI webhook listener for ShareSentinel."""

import logging
import sys
import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from pythonjsonlogger.json import JsonFormatter as jsonlogger_JsonFormatter

from app.config import settings
from app.deduplication import generate_dedup_key, is_duplicate
from app.models import SplunkWebhookPayload
from app.queue import build_queue_job, enqueue_job
from app.validation import ValidationError, validate_payload

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

_log_handler = logging.StreamHandler(sys.stdout)
_formatter = jsonlogger_JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    rename_fields={"asctime": "timestamp", "levelname": "level", "name": "service"},
)
_log_handler.setFormatter(_formatter)

logger = logging.getLogger("webhook-listener")
logger.handlers.clear()
logger.addHandler(_log_handler)
logger.setLevel(settings.log_level)
logger.propagate = False

# Also capture uvicorn access logs in the same format
for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _uv = logging.getLogger(name)
    _uv.handlers.clear()
    _uv.addHandler(_log_handler)
    _uv.setLevel(settings.log_level)
    _uv.propagate = False

# ---------------------------------------------------------------------------
# Redis client (module-level reference, initialized on startup)
# ---------------------------------------------------------------------------

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the active Redis client or raise."""
    if _redis is None:
        raise RuntimeError("Redis client is not initialized")
    return _redis


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    logger.info("Starting webhook listener", extra={"redis_url": settings.redis_url})
    _redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
    )
    try:
        await _redis.ping()
        logger.info("Redis connection established")
    except Exception:
        logger.error("Failed to connect to Redis on startup")
    yield
    if _redis:
        await _redis.aclose()
        logger.info("Redis connection closed")
    _redis = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="ShareSentinel Webhook Listener", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next) -> Response:
    start = time.monotonic()
    response: Response = await call_next(request)
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    logger.info(
        "Request processed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
        },
    )
    return response


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _check_auth(request: Request) -> JSONResponse | None:
    """Return a 401 JSONResponse if auth is enabled and the token is invalid.

    Returns None when auth passes or is disabled.
    """
    if not settings.auth_enabled:
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"error": "Missing or invalid Authorization header"},
        )
    token = auth_header[len("Bearer ") :]
    if token != settings.webhook_auth_secret:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid authentication token"},
        )
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/webhook/splunk")
async def receive_webhook(request: Request) -> JSONResponse:
    """Receive a Splunk webhook, validate, deduplicate, and enqueue."""
    # Auth check
    auth_error = _check_auth(request)
    if auth_error is not None:
        return auth_error

    # Parse body
    try:
        body = await request.json()
    except Exception:
        logger.warning("Malformed JSON body")
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    try:
        payload = SplunkWebhookPayload.model_validate(body)
    except PydanticValidationError as exc:
        logger.warning("Payload validation failed", extra={"errors": str(exc)})
        return JSONResponse(
            status_code=400,
            content={"error": f"Payload validation failed: {exc.error_count()} error(s)"},
        )

    # Validate business rules
    try:
        warnings = validate_payload(payload.result)
    except ValidationError as exc:
        logger.warning(
            "Validation rejected payload",
            extra={"reason": exc.message},
        )
        return JSONResponse(status_code=400, content={"error": exc.message})

    # Generate dedup key / event_id
    event_id = generate_dedup_key(payload.result)

    logger.info(
        "Webhook received",
        extra={
            "event_id": event_id,
            "operation": payload.result.Operation,
            "user_id": payload.result.UserId,
            "file_name": payload.result.SourceFileName,
            "item_type": payload.result.ItemType,
        },
    )

    # Deduplication + enqueue (Redis required)
    try:
        redis_client = get_redis()
        duplicate = await is_duplicate(
            redis_client, event_id, settings.dedup_ttl_seconds
        )
    except Exception as exc:
        logger.error("Redis error during dedup check", extra={"error": str(exc)})
        return JSONResponse(
            status_code=500,
            content={"error": "Internal error: queue unavailable"},
        )

    if duplicate:
        return JSONResponse(
            status_code=200,
            content={"status": "duplicate", "event_id": event_id},
        )

    # Enqueue
    try:
        job = build_queue_job(payload, event_id)
        await enqueue_job(get_redis(), job)
    except Exception as exc:
        logger.error("Redis error during enqueue", extra={"error": str(exc)})
        return JSONResponse(
            status_code=500,
            content={"error": "Internal error: queue unavailable"},
        )

    response_body: dict = {"status": "queued", "event_id": event_id}
    if warnings:
        response_body["warnings"] = warnings

    return JSONResponse(status_code=200, content=response_body)


@app.get("/health")
async def health_check() -> JSONResponse:
    """Health check — verifies Redis connectivity."""
    try:
        redis_client = get_redis()
        await redis_client.ping()
        return JSONResponse(
            status_code=200,
            content={"status": "healthy", "redis_connected": True},
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "redis_connected": False},
        )
