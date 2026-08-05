"""Per-request correlation id, carried via a contextvar and an X-Request-ID header.

A DEBUG log run can be reassembled end-to-end across modules by filtering on
the request id. The middleware sets the contextvar on entry and the response
header on exit; RequestIDFilter copies it onto every log record, and the
format string installed in src/main.py renders it. Call sites do not mention
it at all — a previous attempt prescribed `extra={"request_id": ...}` at each
site, which no production code ever passed.

Work that outlives the request — background warm tasks, streaming response
bodies — runs after the contextvar is reset, so those call chains take an
explicit request_id parameter instead.
"""

import contextvars
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

HEADER = "X-Request-ID"


def current_request_id() -> str | None:
    return _request_id.get()


class RequestIDFilter(logging.Filter):
    """Attach the current request id to every record, or "-" outside a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get(HEADER.lower()) or uuid.uuid4().hex
        token = _request_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[HEADER] = rid
        return response
