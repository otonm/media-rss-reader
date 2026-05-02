"""FastAPI application entry point.

The lifespan context manager runs startup and shutdown logic:
- Builds the HTML string with injected CSS variables (once, at startup)
- Opens the persistent database connection used by the scheduler
- Applies schema and pending migrations
- Starts the background scheduler (which fires an immediate OPML sync + feed refresh)
- On shutdown: stops the scheduler and closes the database connection

The index route serves the pre-built HTML string from app.state to avoid
re-reading the file on every request.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api import feeds, items, media
from src.config import settings
from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema
from src.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=settings.log_level.upper())
# Suppress noisy third-party loggers that produce per-request output at INFO.
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_static_dir = Path(__file__).parent / "static"
_index_path = _static_dir / "index.html"


def _build_html() -> str:
    """Read index.html and inject backend config values as CSS custom properties.

    The <!-- SLIDESHOW_TRANSITION --> comment is replaced with a <style> block
    so the frontend can read these values synchronously via getComputedStyle()
    without a separate API call. The result is cached for the process lifetime.
    """
    if not _index_path.exists():
        return ""
    style = (
        f"<style>:root{{"
        f"--slideshow-transition-ms:{settings.slideshow_transition_ms}ms;"
        f"--image-display-delay-ms:{settings.image_display_delay_ms}ms;"
        f"--prefetch-ahead:{settings.prefetch_ahead};"
        f"--auto-scroll-speed:{settings.auto_scroll_speed}"
        f"}}</style>"
    )
    return _index_path.read_text().replace("<!-- SLIDESHOW_TRANSITION -->", style)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Build HTML once; store on app.state for the index route to serve.
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
app.include_router(feeds.router, prefix="/api")
app.include_router(items.router, prefix="/api")
app.include_router(media.router, prefix="/api")

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    """Serve the pre-built single-page app shell."""
    return request.app.state.html
