"""Starlette middleware: HTTPS enforcement and session validation."""

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from src.auth.session import SESSION_COOKIE, verify_session
from src.config import settings

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/health"
_AUTH_FREE_PREFIXES = ("/static/",)
_AUTH_FREE_EXACT = {"/login", "/setup"}


def _is_auth_free(path: str) -> bool:
    if path in _AUTH_FREE_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _AUTH_FREE_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        # Health endpoint bypasses all checks — internal liveness probe only.
        if path == _HEALTH_PATH:
            return await call_next(request)

        # Assumes a trusted reverse proxy always sets X-Forwarded-Proto;
        # do not expose this service directly to the internet.
        if request.headers.get("x-forwarded-proto") != "https":
            logger.debug(f"dispatch path={path} rejected: not HTTPS")
            return Response("HTTPS required.", status_code=403)

        if _is_auth_free(path):
            logger.debug(f"dispatch path={path} auth-free, passing through")
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE, "")
        if not verify_session(token, settings.auth_secret_key):
            logger.debug(f"dispatch path={path} no valid session, redirecting to /login")
            return RedirectResponse("/login", status_code=302)

        logger.debug(f"dispatch path={path} session valid")
        return await call_next(request)
