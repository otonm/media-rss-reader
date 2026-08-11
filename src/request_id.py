"""Per-request correlation id, carried via a contextvar and the X-Request-ID header.

The middleware sets the contextvar on entry and the response header on exit;
RequestIDFilter copies it onto every log record, and the format string
installed in src/main.py renders it, so a DEBUG run can be reassembled
end-to-end across modules by filtering on the id.

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
        # nosniff rides along here rather than in a middleware of its own: this
        # one already wraps AuthMiddleware, every router and the /static mount,
        # and a third BaseHTTPMiddleware would put another task group around the
        # proxy's StreamingResponse. setdefault, not assignment: a route with a
        # reason to set its own value keeps it.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
