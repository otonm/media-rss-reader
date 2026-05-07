"""Starlette middleware: HTTPS enforcement and session validation."""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from src.auth.session import SESSION_COOKIE, verify_session
from src.config import settings

_AUTH_FREE_PREFIXES = ("/static/",)
_AUTH_FREE_EXACT = {"/login", "/setup"}


def _is_auth_free(path: str) -> bool:
    if path in _AUTH_FREE_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _AUTH_FREE_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.get("x-forwarded-proto") != "https":
            return Response("HTTPS required.", status_code=403)

        if _is_auth_free(request.url.path):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE, "")
        if not verify_session(token, settings.auth_secret_key.get_secret_value()):
            return RedirectResponse("/login", status_code=302)

        return await call_next(request)
