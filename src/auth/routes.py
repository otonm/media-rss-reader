"""Authentication routes: /login, /setup, /logout."""

import html as _html
import logging
import secrets
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Form, HTTPException, Request
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
from src.db.connection import DbDep, write_transaction

logger = logging.getLogger(__name__)
router = APIRouter()

_static = Path(__file__).parent.parent / "static"

# Cache template files at module load time to avoid repeated disk reads.
_login_html: str = (_static / "login.html").read_text()
_setup_html: str = (_static / "setup.html").read_text()

_lockout = LockoutTracker(
    max_attempts=settings.auth_lockout_attempts,
    lockout_seconds=settings.auth_lockout_minutes * 60,
)


def _client_ip(request: Request) -> str:
    # Assumes a trusted reverse proxy always sets X-Forwarded-For;
    # do not expose this service directly to the internet.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _set_session_cookie(response: Response) -> None:
    token = sign_session(settings.auth_secret_key)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _set_setup_cookie(response: Response, totp_secret: str) -> None:
    token = sign_setup_cookie(totp_secret, settings.auth_secret_key)
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
    *,
    db: DbDep,
) -> Response:
    ip = _client_ip(request)
    logger.debug(f"post_login ip={ip} username_provided={bool(username)}")

    if _lockout.is_locked(ip):
        logger.debug(f"post_login ip={ip} rejected: locked out")
        return Response("Too many failed attempts. Try again later.", status_code=429)

    username_ok = secrets.compare_digest(username, settings.auth_username)
    password_ok = secrets.compare_digest(password, settings.auth_password)

    if not (username_ok and password_ok):
        _lockout.record_failure(ip)
        logger.debug(f"post_login ip={ip} invalid credentials")
        return Response("Invalid credentials.", status_code=401)

    stored_secret = await _load_totp_secret(db)

    if stored_secret is None:
        logger.debug(f"post_login ip={ip} no TOTP configured, redirecting to /setup")
        new_secret = totp_module.generate_secret()
        response = RedirectResponse("/setup", status_code=303)
        _set_setup_cookie(response, new_secret)
        return response

    if not totp_module.verify_code(stored_secret, totp_code):
        _lockout.record_failure(ip)
        logger.debug(f"post_login ip={ip} TOTP verification failed")
        return Response("Invalid credentials.", status_code=401)

    _lockout.reset(ip)
    logger.debug(f"post_login ip={ip} success, issuing session cookie")
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response)
    return response


@router.get("/setup")
async def get_setup(request: Request, db: DbDep) -> Response:
    logger.debug("get_setup entered")
    if await _load_totp_secret(db) is not None:
        logger.debug("get_setup TOTP already configured, redirecting to /login")
        return RedirectResponse("/login", status_code=302)

    setup_token = request.cookies.get(SETUP_COOKIE, "")
    secret = verify_setup_cookie(setup_token, settings.auth_secret_key)
    if secret is None:
        logger.debug("get_setup setup cookie missing or expired")
        return Response("Setup session expired. Please log in again.", status_code=403)

    logger.debug("get_setup rendering setup page")
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
    *,
    db: DbDep,
) -> Response:
    ip = _client_ip(request)
    logger.debug(f"post_setup ip={ip}")
    if _lockout.is_locked(ip):
        logger.debug(f"post_setup ip={ip} rejected: locked out")
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    if await _load_totp_secret(db) is not None:
        logger.debug("post_setup TOTP already configured, redirecting to /login")
        return RedirectResponse("/login", status_code=302)

    setup_token = request.cookies.get(SETUP_COOKIE, "")
    secret = verify_setup_cookie(setup_token, settings.auth_secret_key)
    if secret is None:
        logger.debug("post_setup setup cookie missing or expired")
        return Response("Setup session expired. Please log in again.", status_code=403)

    if not totp_module.verify_code(secret, totp_code):
        _lockout.record_failure(ip)
        logger.debug(f"post_setup ip={ip} TOTP verification failed")
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
    logger.debug(f"post_setup ip={ip} success, persisting TOTP secret")
    async with write_transaction(db):
        await db.execute("INSERT OR REPLACE INTO auth_config (key, value) VALUES ('totp_secret', ?)", (secret,))

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response)
    response.delete_cookie(SETUP_COOKIE)
    return response


@router.post("/logout")
async def logout() -> Response:
    logger.debug("logout clearing session cookie")
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
