"""Per-request correlation id, carried via a contextvar and an X-Request-ID header.

A DEBUG log run can be reassembled end-to-end across modules by filtering on
the request id. The middleware sets the contextvar on entry and the response
header on exit; log lines use `extra={"request_id": current_request_id()}` so
the id is attached without being interpolated into every f-string.
"""

import contextvars
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

HEADER = "X-Request-ID"


def current_request_id() -> str | None:
    return _request_id.get()


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
