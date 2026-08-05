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
from src.db.migrations import backfill_seen_media, run_migrations
from src.db.schema import create_schema
from src.request_id import RequestIDFilter, RequestIDMiddleware
from src.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
)
# basicConfig is a no-op if handlers already exist (pytest's logging plugin,
# a previous import); set the filter and format on them regardless.
_log_fmt = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIDFilter())
    _handler.setFormatter(logging.Formatter(_log_fmt))
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
        f"--ui-debug:{settings.ui_debug};"
        f"}}</style>"
    )
    # ponytail: per-startup token — the constant package version let browser/SW
    # caches serve stale assets across deploys. Content hash if bytes must
    # survive restarts.
    v = str(int(time.time()))
    html = _index_path.read_text().replace("<!-- CONFIG_VARS -->", style).replace("{{VERSION}}", v)
    logger.debug(f"_build_html: injected {style} (asset version {v})")
    if settings.ui_debug:
        logger.info("UI_DEBUG=1 — the browser overlay is enabled")
    # The injected block must land after the stylesheet link or style.css's
    # :root defaults win the cascade and every env-supplied value is ignored.
    if "style.css" in html and html.index("style.css") > html.index("--ui-debug"):
        logger.error("index.html injects CSS variables BEFORE style.css — env config will be ignored")
    return html


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.html = _build_html()
    db = await open_db(settings.db_path)
    await create_schema(db)
    await run_migrations(db)
    await backfill_seen_media(db)
    app.state.db = db
    # The scheduler gets its own connection. sqlite3's implicit transaction is
    # per connection, not per coroutine, and sync.py writes many rows before it
    # commits — sharing meant a mark_seen commit could land mid-refresh and
    # commit a partial feed, or its rollback discard one.
    scheduler_db = await open_db(settings.db_path)
    await start_scheduler(scheduler_db)
    yield
    await stop_scheduler()
    await scheduler_db.close()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestIDMiddleware)
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
