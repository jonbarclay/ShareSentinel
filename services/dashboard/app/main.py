"""FastAPI dashboard application — serves API + static React build."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import events, verdicts, stats, audit
from .config import DATABASE_URL, ALLOWED_ORIGINS

STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


async def _init_conn(conn):
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10, init=_init_conn
    )
    yield
    await app.state.db.close()


app = FastAPI(title="ShareSentinel Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
