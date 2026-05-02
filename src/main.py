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
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_static_dir = Path(__file__).parent / "static"
_index_path = _static_dir / "index.html"


def _build_html() -> str:
    if not _index_path.exists():
        return ""
    style = (
        f"<style>:root{{"
        f"--slideshow-transition-ms:{settings.slideshow_transition_ms}ms;"
        f"--image-display-delay-ms:{settings.image_display_delay_ms}ms;"
        f"--prefetch-ahead:{settings.prefetch_ahead}"
        f"}}</style>"
    )
    return _index_path.read_text().replace("<!-- SLIDESHOW_TRANSITION -->", style)


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
app.include_router(feeds.router, prefix="/api")
app.include_router(items.router, prefix="/api")
app.include_router(media.router, prefix="/api")

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    return request.app.state.html
