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


def test_filter_attaches_the_current_request_id() -> None:
    """No production code passes extra={"request_id": ...} — the docstring
    prescribed a mechanism nothing used, and basicConfig set no format, so the
    attribute would have been discarded anyway. One filter covers all 23 sites.
    """
    import logging

    from src.request_id import RequestIDFilter, _request_id

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    token = _request_id.set("deadbeef")
    try:
        assert RequestIDFilter().filter(record) is True
        assert record.request_id == "deadbeef"
    finally:
        _request_id.reset(token)


def test_filter_supplies_a_placeholder_outside_a_request() -> None:
    """Startup and scheduler records go through the same handler; the format
    string must not KeyError on them."""
    import logging

    from src.request_id import RequestIDFilter

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    RequestIDFilter().filter(record)
    assert record.request_id == "-"


def test_root_handler_renders_the_request_id() -> None:
    import logging

    import src.main  # noqa: F401  — importing configures logging

    handlers = logging.getLogger().handlers
    assert handlers, "basicConfig must have installed a handler"
    assert any(any(f.__class__.__name__ == "RequestIDFilter" for f in h.filters) for h in handlers), (
        "every record reaching the root handler must carry a request_id"
    )
    assert any("%(request_id)s" in (h.formatter._fmt or "") for h in handlers if h.formatter), (
        "the format string must actually render it"
    )
