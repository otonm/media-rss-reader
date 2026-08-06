import httpx
from fastapi import FastAPI, Request

from src.http_client import get_http, get_status_http


async def test_get_http_returns_the_app_state_client() -> None:
    app = FastAPI()
    client = httpx.AsyncClient()
    app.state.http = client
    request = Request({"type": "http", "app": app, "headers": []})

    assert await get_http(request) is client
    await client.aclose()


async def test_get_status_http_returns_the_separate_status_client() -> None:
    app = FastAPI()
    media = httpx.AsyncClient()
    status = httpx.AsyncClient()
    app.state.http = media
    app.state.http_status = status
    request = Request({"type": "http", "app": app, "headers": []})

    assert await get_status_http(request) is status
    assert await get_status_http(request) is not media
    await media.aclose()
    await status.aclose()
