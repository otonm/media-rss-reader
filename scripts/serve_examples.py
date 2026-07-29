#!/usr/bin/env python3
"""Serve the media-rss-reader WebUI pre-loaded with 4 hardcoded example items.

Start:  uv run python scripts/serve_examples.py
Expose: tailscale funnel <port>
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

THIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = THIS_DIR.parent
EXAMPLES_DIR = PROJECT_DIR / "examples"
STATIC_DIR = PROJECT_DIR / "src" / "static"
INDEX_PATH = STATIC_DIR / "index.html"

ITEMS: list[dict[str, Any]] = [
    {
        "id": "example-item-1",
        "feed_id": "example-feed",
        "guid": "example-item-1-guid",
        "title": "Video \u2014 M2 Res 480p",
        "media_url": "/examples/item1/m2-res_480p.mp4",
        "media_type": "video",
        "media": [{"url": "/examples/item1/m2-res_480p.mp4", "type": "video"}],
        "pub_date": "2024-01-01T12:00:00+00:00",
        "fetched_at": "2024-01-01T12:00:00+00:00",
        "seen_at": None,
    },
    {
        "id": "example-item-2",
        "feed_id": "example-feed",
        "guid": "example-item-2-guid",
        "title": "Gallery \u2014 3 Wallpapers",
        "media_url": "/examples/item2/mountains-wallpaper-1920x1080-v0-cuw6h7wz4gfh1.webp",
        "media_type": "image",
        "media": [
            {"url": "/examples/item2/mountains-wallpaper-1920x1080-v0-cuw6h7wz4gfh1.webp", "type": "image"},
            {
                "url": "/examples/item2/saturn-rings-nasa-as-photographed-by-the-cassini-huygens-v0-zglu3e470ifh1.webp",
                "type": "image",
            },
            {"url": "/examples/item2/mana-confluence-2560x1440-ai-modified-v0-65aef2h6p6fh1.webp", "type": "image"},
        ],
        "pub_date": "2024-01-01T13:00:00+00:00",
        "fetched_at": "2024-01-01T13:00:00+00:00",
        "seen_at": None,
    },
    {
        "id": "example-item-3",
        "feed_id": "example-feed",
        "guid": "example-item-3-guid",
        "title": "GIF \u2014 Animation",
        "media_url": "/examples/item3/44ca5hkshlfh1.gif",
        "media_type": "gif",
        "media": [{"url": "/examples/item3/44ca5hkshlfh1.gif", "type": "gif"}],
        "pub_date": "2024-01-01T14:00:00+00:00",
        "fetched_at": "2024-01-01T14:00:00+00:00",
        "seen_at": None,
    },
    {
        "id": "example-item-4",
        "feed_id": "example-feed",
        "guid": "example-item-4-guid",
        "title": "Image \u2014 Windows XP Parody",
        "media_url": ("/examples/item4/a-parody-of-the-default-windows-xp-wallpaper-2480-x-1754-v0-ehm376x7tveh1.webp"),
        "media_type": "image",
        "media": [
            {
                "url": (
                    "/examples/item4/a-parody-of-the-default-windows-xp-wallpaper-2480-x-1754-v0-ehm376x7tveh1.webp"
                ),
                "type": "image",
            }
        ],
        "pub_date": "2024-01-01T15:00:00+00:00",
        "fetched_at": "2024-01-01T15:00:00+00:00",
        "seen_at": None,
    },
]

_MIME_MAP: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def _guess_mime(suffix: str) -> str:
    return _MIME_MAP.get(suffix.lower(), "application/octet-stream")


def _build_html() -> str:
    if not INDEX_PATH.exists():
        return "index.html not found"
    style = "<style>:root{--feed-initial-count:5;--image-autoscroll-delay-s:2;}</style>"
    return INDEX_PATH.read_text().replace("<!-- CONFIG_VARS -->", style).replace("{{VERSION}}", "dev")


app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(_request: Request) -> str:
    return _build_html()


@app.get("/api/items")
async def list_items(
    unseen: bool = False, page: int = 0, size: int = 50, feed_id: str | None = None
) -> list[dict[str, Any]]:
    items = ITEMS
    if unseen:
        items = [i for i in items if i["seen_at"] is None]
    if feed_id is not None:
        items = [i for i in items if i["feed_id"] == feed_id]
    return items[page * size : page * size + size]


@app.get("/api/items/count")
async def count_items(unseen: bool = True, feed_id: str | None = None) -> dict[str, int]:
    items = ITEMS
    if unseen:
        items = [i for i in items if i["seen_at"] is None]
    if feed_id is not None:
        items = [i for i in items if i["feed_id"] == feed_id]
    return {"count": len(items)}


@app.post("/api/items/{item_id}/seen")
async def mark_seen(item_id: str) -> dict[str, str]:
    for it in ITEMS:
        if it["id"] == item_id:
            it["seen_at"] = datetime.now(UTC).isoformat()
            return {"seen_at": it["seen_at"]}
    raise HTTPException(status_code=404, detail="Not found")


@app.post("/api/prefetch/hint")
async def _prefetch_hint() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/feeds")
async def list_feeds() -> list[dict[str, Any]]:
    return [
        {
            "id": "example-feed",
            "title": "Example Media Feed",
            "url": "http://localhost/feed.xml",
            "item_count": len(ITEMS),
            "unseen_count": sum(1 for i in ITEMS if i["seen_at"] is None),
            "last_fetched_at": "2024-01-01T12:00:00+00:00",
        }
    ]


@app.get("/api/reddit-feeds/status")
async def _reddit_feeds_status() -> dict[str, Any]:
    return {"feeds": [], "last_run": None}


@app.get("/api/status")
async def _status() -> dict[str, Any]:
    return {
        "feeds": 1,
        "items_total": len(ITEMS),
        "items_unseen": sum(1 for i in ITEMS if i["seen_at"] is None),
        "cache_size_mb": 0,
        "last_opml_sync": datetime.now(UTC).isoformat(),
    }


@app.get("/api/media/proxy")
async def proxy_media(url: str = Query(...)) -> FileResponse:
    if url.startswith("/examples/"):
        file_path = PROJECT_DIR / url.lstrip("/")
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {url}")
        return FileResponse(str(file_path), media_type=_guess_mime(file_path.suffix))
    raise HTTPException(status_code=400, detail=f"Cannot proxy URL: {url}")


@app.get("/health")
async def _health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serve Media RSS Reader examples")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
