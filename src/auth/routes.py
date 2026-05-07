"""Authentication routes: /login, /setup, /logout."""

import html as _html
import secrets
from pathlib import Path
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.auth import totp as totp_module
from src.auth.lockout import LockoutTracker
from src.auth.session import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SETUP_COOKIE,
    SETUP_MAX_AGE,
    sign_session,
    sign_setup_cookie,
    verify_setup_cookie,
)
from src.config import settings
from src.db.connection import get_db

router = APIRouter()

_static = Path(__file__).parent.parent / "static"

# Cache template files at module load time to avoid repeated disk reads.
_login_html: str = (_static / "login.html").read_text()
_setup_html: str = (_static / "setup.html").read_text()

_lockout = LockoutTracker(
    max_attempts=settings.auth_lockout_attempts,
    lockout_seconds=settings.auth_lockout_minutes * 60,
)

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


def _client_ip(request: Request) -> str:
    # Assumes a trusted reverse proxy always sets X-Forwarded-For;
    # do not expose this service directly to the internet.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _set_session_cookie(response: Response) -> None:
    token = sign_session(settings.auth_secret_key.get_secret_value())
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _set_setup_cookie(response: Response, totp_secret: str) -> None:
    token = sign_setup_cookie(totp_secret, settings.auth_secret_key.get_secret_value())
    response.set_cookie(
        SETUP_COOKIE,
        token,
        max_age=SETUP_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )


async def _load_totp_secret(db: aiosqlite.Connection) -> str | None:
    async with db.execute("SELECT value FROM auth_config WHERE key = 'totp_secret'") as cur:
        row = await cur.fetchone()
    return row[0] if row else None


@router.get("/login", response_class=HTMLResponse)
async def get_login() -> str:
    return _login_html


@router.post("/login")
async def post_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(default=""),
    db: _DbDep = None,  # type: ignore[assignment]
) -> Response:
    ip = _client_ip(request)

    if _lockout.is_locked(ip):
        return Response("Too many failed attempts. Try again later.", status_code=429)

    username_ok = secrets.compare_digest(username, settings.auth_username)
    password_ok = secrets.compare_digest(password, settings.auth_password.get_secret_value())

    if not (username_ok and password_ok):
        _lockout.record_failure(ip)
        return Response("Invalid credentials.", status_code=401)

    stored_secret = await _load_totp_secret(db)

    if stored_secret is None:
        new_secret = totp_module.generate_secret()
        response = RedirectResponse("/setup", status_code=303)
        _set_setup_cookie(response, new_secret)
        return response

    if not totp_module.verify_code(stored_secret, totp_code):
        _lockout.record_failure(ip)
        return Response("Invalid credentials.", status_code=401)

    _lockout.reset(ip)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response)
    return response


@router.get("/setup")
async def get_setup(request: Request, db: _DbDep = None) -> Response:  # type: ignore[assignment]
    if await _load_totp_secret(db) is not None:
        return RedirectResponse("/login", status_code=302)

    setup_token = request.cookies.get(SETUP_COOKIE, "")
    secret = verify_setup_cookie(setup_token, settings.auth_secret_key.get_secret_value())
    if secret is None:
        return Response("Setup session expired. Please log in again.", status_code=403)

    uri = totp_module.build_uri(secret, settings.auth_username)
    html = (
        _setup_html.replace("{{TOTP_URI}}", _html.escape(uri))
        .replace("{{TOTP_SECRET}}", _html.escape(secret))
        .replace("{{ERROR}}", "")
    )
    return HTMLResponse(html)


@router.post("/setup")
async def post_setup(
    request: Request,
    totp_code: str = Form(...),
    db: _DbDep = None,  # type: ignore[assignment]
) -> Response:
    ip = _client_ip(request)
    if _lockout.is_locked(ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    if await _load_totp_secret(db) is not None:
        return RedirectResponse("/login", status_code=302)

    setup_token = request.cookies.get(SETUP_COOKIE, "")
    secret = verify_setup_cookie(setup_token, settings.auth_secret_key.get_secret_value())
    if secret is None:
        return Response("Setup session expired. Please log in again.", status_code=403)

    if not totp_module.verify_code(secret, totp_code):
        _lockout.record_failure(ip)
        uri = totp_module.build_uri(secret, settings.auth_username)
        html = (
            _setup_html.replace("{{TOTP_URI}}", _html.escape(uri))
            .replace("{{TOTP_SECRET}}", _html.escape(secret))
            .replace("{{ERROR}}", "Invalid code. Try again.")
        )
        resp = HTMLResponse(html)
        _set_setup_cookie(resp, secret)
        return resp

    _lockout.reset(ip)
    await db.execute("INSERT OR REPLACE INTO auth_config (key, value) VALUES ('totp_secret', ?)", (secret,))
    await db.commit()

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response)
    response.delete_cookie(SETUP_COOKIE)
    return response


@router.post("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
