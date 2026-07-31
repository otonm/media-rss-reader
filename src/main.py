"""FastAPI application entry point."""

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api import feeds, items, media, reddit_feeds
from src.auth import routes as auth_routes
from src.auth.middleware import AuthMiddleware
from src.config import settings
from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema
from src.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=settings.log_level.upper())
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_static_dir = Path(__file__).parent / "static"
_index_path = _static_dir / "index.html"


def _build_html() -> str:
    style = (
        f"<style>:root{{"
        f"--feed-initial-count:{settings.feed_initial_count};"
        f"--image-autoscroll-delay-s:{settings.image_autoscroll_delay_s};"
        f"}}</style>"
    )
    # ponytail: per-startup token — the constant package version let browser/SW
    # caches serve stale assets across deploys. Content hash if bytes must
    # survive restarts.
    v = str(int(time.time()))
    return _index_path.read_text().replace("<!-- CONFIG_VARS -->", style).replace("{{VERSION}}", v)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.html = _build_html()
    db = await open_db(settings.db_path)
    await create_schema(db)
    await run_migrations(db)
    app.state.db = db
    await start_scheduler(db)
    yield
    await stop_scheduler()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(auth_routes.router)
app.include_router(feeds.router, prefix="/api")
app.include_router(items.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(reddit_feeds.router, prefix="/api")

app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    return request.app.state.html
