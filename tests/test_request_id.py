import asyncio
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.request_id import RequestIDMiddleware, current_request_id


@pytest.mark.asyncio
async def test_request_id_header_set() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/")
    async def _root() -> dict[str, str]:
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) == 32


@pytest.mark.asyncio
async def test_concurrent_requests_get_distinct_ids() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/")
    async def _root() -> dict[str, str]:
        await asyncio.sleep(0)
        return {"rid": current_request_id() or ""}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1, r2 = await asyncio.gather(c.get("/"), c.get("/"))
    assert r1.json()["rid"] != r2.json()["rid"]


@pytest.mark.asyncio
async def test_handler_log_line_includes_request_id(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    logger = logging.getLogger("test_rid")

    @app.get("/")
    async def _root() -> dict[str, str]:
        logger.info("handling", extra={"request_id": current_request_id()})
        return {"status": "ok"}

    caplog.set_level(logging.INFO, logger="test_rid")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    rec = next(r for r in caplog.records if r.getMessage() == "handling")
    assert getattr(rec, "request_id", None) is not None
    assert len(rec.request_id) == 32
