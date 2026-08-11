"""The app-scoped HTTP clients, as FastAPI dependencies.

Two clients, not one. The media proxy, the feed scheduler and the prefetcher
share the first; the reddit-feeds status poll gets the second, with a small
connection limit, so an absent or hung companion service cannot consume the
pool the reader depends on.
The status modal polls at 1 Hz with no in-flight guard, and httpx's read
timeout is the gap between reads rather than a whole-request budget, so a
trickling companion on the shared pool could starve every media request.

Dependencies rather than a module global: src/api must not import
src/scheduler just to borrow a socket pool, and one dependency_overrides line
in a test replaces a monkeypatch at every call site.
"""

import logging
from typing import Annotated

import httpx
from fastapi import Depends, Request

logger = logging.getLogger(__name__)


async def get_http(request: Request) -> httpx.AsyncClient:
    """The shared client: media proxy and prefetch warm tasks."""
    return request.app.state.http


async def get_status_http(request: Request) -> httpx.AsyncClient:
    """The reddit-feeds poll's own client, capped at 2 connections."""
    return request.app.state.http_status


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
StatusDep = Annotated[httpx.AsyncClient, Depends(get_status_http)]
