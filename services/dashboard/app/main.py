"""FastAPI dashboard application — serves API + static React build."""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import events, verdicts, stats, audit
from .config import (
    DATABASE_URL, ALLOWED_ORIGINS, AUTH_ENABLED, REDIS_URL, SESSION_REDIS_DB,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


async def _init_conn(conn):
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # PostgreSQL pool
    app.state.db = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10, init=_init_conn
    )

    # Redis for sessions (only when auth is enabled)
    app.state.session_redis = None
    app.state.oidc_config_cache = None
    app.state.jwks_cache = None

    if AUTH_ENABLED:
        import redis.asyncio as aioredis
        # Parse base URL and switch to session DB
        redis_url = REDIS_URL
        # Replace DB number if present, otherwise append
        if redis_url.rstrip("/").rsplit("/", 1)[-1].isdigit():
            redis_url = redis_url.rstrip("/").rsplit("/", 1)[0] + f"/{SESSION_REDIS_DB}"
        else:
            redis_url = redis_url.rstrip("/") + f"/{SESSION_REDIS_DB}"
        app.state.session_redis = aioredis.from_url(redis_url, decode_responses=True)
        logger.info("Session Redis connected (DB %d)", SESSION_REDIS_DB)

    yield

    # Cleanup
    await app.state.db.close()
    if app.state.session_redis is not None:
        await app.state.session_redis.aclose()


app = FastAPI(title="ShareSentinel Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register auth middleware and routes when SSO is configured
if AUTH_ENABLED:
    from .auth import AuthMiddleware, router as auth_router
    app.add_middleware(AuthMiddleware)
    app.include_router(auth_router)

app.include_router(events.router, prefix="/api")
app.include_router(verdicts.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(audit.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if STATIC_DIR.is_dir():
    INDEX_HTML = STATIC_DIR / "index.html"
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(request: Request, full_path: str):
        """Serve index.html for all non-API routes (React Router SPA)."""
        file = STATIC_DIR / full_path
        if file.is_file():
            return FileResponse(file)
        return HTMLResponse(
            content=INDEX_HTML.read_bytes(),
            media_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
