"""Authentication tests."""

import time

import aiosqlite
import pytest
from httpx import AsyncClient as HttpxAsyncClient

from src.auth.session import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SETUP_COOKIE,
    SETUP_MAX_AGE,
    sign_session,
    sign_setup_cookie,
    verify_session,
    verify_setup_cookie,
)
from src.config import settings


# ---------------------------------------------------------------------------
# session.py tests
# ---------------------------------------------------------------------------

def test_session_constants() -> None:
    assert SESSION_COOKIE == "session"
    assert SETUP_COOKIE == "totp_setup"
    assert SESSION_MAX_AGE == 604800
    assert SETUP_MAX_AGE == 600


def test_sign_and_verify_session() -> None:
    key = "test-secret-key"
    token = sign_session(key)
    assert isinstance(token, str)
    assert len(token) > 0
    assert verify_session(token, key) is True


def test_verify_session_wrong_key() -> None:
    token = sign_session("correct-key")
    assert verify_session(token, "wrong-key") is False


def test_verify_session_tampered_token() -> None:
    assert verify_session("not.a.valid.token", "any-key") is False


def test_sign_and_verify_setup_cookie() -> None:
    key = "test-key"
    secret = "JBSWY3DPEHPK3PXP"
    token = sign_setup_cookie(secret, key)
    result = verify_setup_cookie(token, key)
    assert result == secret


def test_verify_setup_cookie_wrong_key() -> None:
    token = sign_setup_cookie("MYSECRET", "correct-key")
    assert verify_setup_cookie(token, "wrong-key") is None


def test_verify_setup_cookie_tampered() -> None:
    assert verify_setup_cookie("garbage", "any-key") is None


# ---------------------------------------------------------------------------
# lockout.py tests
# ---------------------------------------------------------------------------

from src.auth.lockout import LockoutTracker


def test_lockout_not_locked_initially() -> None:
    tracker = LockoutTracker(max_attempts=3, lockout_seconds=60)
    assert tracker.is_locked("1.2.3.4") is False


def test_lockout_locks_after_max_attempts() -> None:
    tracker = LockoutTracker(max_attempts=3, lockout_seconds=60)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked("1.2.3.4") is False  # not yet
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked("1.2.3.4") is True


def test_lockout_reset_clears_entry() -> None:
    tracker = LockoutTracker(max_attempts=2, lockout_seconds=60)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked("1.2.3.4") is True
    tracker.reset("1.2.3.4")
    assert tracker.is_locked("1.2.3.4") is False


def test_lockout_different_ips_are_independent() -> None:
    tracker = LockoutTracker(max_attempts=2, lockout_seconds=60)
    tracker.record_failure("1.1.1.1")
    tracker.record_failure("1.1.1.1")
    assert tracker.is_locked("1.1.1.1") is True
    assert tracker.is_locked("2.2.2.2") is False


def test_lockout_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module
    tracker = LockoutTracker(max_attempts=2, lockout_seconds=5)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked("1.2.3.4") is True
    # Capture the real time before patching, then fast-forward past the 5-second lockout.
    # Do NOT call time_module.monotonic() inside the lambda — it would recurse into itself.
    real_now = time_module.monotonic()
    monkeypatch.setattr(time_module, "monotonic", lambda: real_now + 10)
    assert tracker.is_locked("1.2.3.4") is False


# ---------------------------------------------------------------------------
# totp.py tests
# ---------------------------------------------------------------------------

import pyotp

from src.auth import totp as totp_module


def test_generate_secret_is_valid_base32() -> None:
    secret = totp_module.generate_secret()
    assert isinstance(secret, str)
    assert len(secret) >= 16
    # Valid base32 — pyotp accepts it without raising
    pyotp.TOTP(secret).now()


def test_generate_secret_is_unique() -> None:
    assert totp_module.generate_secret() != totp_module.generate_secret()


def test_build_uri_contains_secret_and_issuer() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    uri = totp_module.build_uri(secret, "admin")
    assert "otpauth://totp/" in uri
    assert secret in uri
    assert "MediaRSSReader" in uri
    assert "admin" in uri


def test_verify_code_accepts_current_code() -> None:
    secret = totp_module.generate_secret()
    code = pyotp.TOTP(secret).now()
    assert totp_module.verify_code(secret, code) is True


def test_verify_code_rejects_wrong_code() -> None:
    secret = totp_module.generate_secret()
    assert totp_module.verify_code(secret, "000000") is False


def test_verify_code_rejects_empty_string() -> None:
    secret = totp_module.generate_secret()
    assert totp_module.verify_code(secret, "") is False


# ---------------------------------------------------------------------------
# Route integration tests (require auth_client / authed_client fixtures)
# ---------------------------------------------------------------------------

import pyotp  # noqa: E402 (already imported above, but re-stating for clarity)


async def _insert_totp_secret(db: aiosqlite.Connection, secret: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO auth_config (key, value) VALUES ('totp_secret', ?)", (secret,)
    )
    await db.commit()


# --- login page ---

async def test_login_page_accessible(auth_client: HttpxAsyncClient) -> None:
    r = await auth_client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


# --- POST /login normal flow ---

async def test_login_success(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    await _insert_totp_secret(db, secret)
    code = pyotp.TOTP(secret).now()
    r = await auth_client.post(
        "/login", data={"username": "admin", "password": "hunter2", "totp_code": code}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "session" in r.cookies


async def test_login_wrong_password(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    await _insert_totp_secret(db, secret)
    r = await auth_client.post(
        "/login", data={"username": "admin", "password": "wrong", "totp_code": "000000"}
    )
    assert r.status_code == 401
    assert "session" not in r.cookies


async def test_login_wrong_totp(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    await _insert_totp_secret(db, secret)
    r = await auth_client.post(
        "/login", data={"username": "admin", "password": "hunter2", "totp_code": "000000"}
    )
    assert r.status_code == 401
    assert "session" not in r.cookies


async def test_login_lockout(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    await _insert_totp_secret(db, secret)
    for _ in range(5):
        await auth_client.post(
            "/login", data={"username": "admin", "password": "wrong", "totp_code": "000000"}
        )
    code = pyotp.TOTP(secret).now()
    r = await auth_client.post(
        "/login", data={"username": "admin", "password": "hunter2", "totp_code": code}
    )
    assert r.status_code == 429


async def test_lockout_resets_on_success(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    await _insert_totp_secret(db, secret)
    # 4 failures (one below lockout threshold)
    for _ in range(4):
        await auth_client.post(
            "/login", data={"username": "admin", "password": "wrong", "totp_code": "000000"}
        )
    # Successful login resets counter
    code = pyotp.TOTP(secret).now()
    r = await auth_client.post(
        "/login", data={"username": "admin", "password": "hunter2", "totp_code": code}
    )
    assert r.status_code == 303
    # Another failure should not immediately lock (counter was reset)
    r2 = await auth_client.post(
        "/login", data={"username": "admin", "password": "wrong", "totp_code": "000000"}
    )
    assert r2.status_code == 401  # not 429


# --- first-login / setup flow ---

async def test_setup_flow_first_login(auth_client: HttpxAsyncClient) -> None:
    """No TOTP in DB → POST /login redirects to /setup with a setup cookie."""
    r = await auth_client.post(
        "/login", data={"username": "admin", "password": "hunter2", "totp_code": ""}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"
    assert SETUP_COOKIE in r.cookies


async def test_setup_page_shows_secret(
    auth_client: HttpxAsyncClient,
) -> None:
    secret = pyotp.random_base32()
    setup_token = sign_setup_cookie(secret, settings.auth_secret_key.get_secret_value())
    auth_client.cookies.set(SETUP_COOKIE, setup_token)
    r = await auth_client.get("/setup")
    assert r.status_code == 200
    assert secret in r.text
    assert "otpauth://" in r.text


async def test_setup_confirms_totp(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    setup_token = sign_setup_cookie(secret, settings.auth_secret_key.get_secret_value())
    auth_client.cookies.set(SETUP_COOKIE, setup_token)
    code = pyotp.TOTP(secret).now()
    r = await auth_client.post("/setup", data={"totp_code": code})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "session" in r.cookies
    # Secret is persisted in DB
    async with db.execute("SELECT value FROM auth_config WHERE key = 'totp_secret'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == secret


async def test_setup_wrong_totp_does_not_persist(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    setup_token = sign_setup_cookie(secret, settings.auth_secret_key.get_secret_value())
    auth_client.cookies.set(SETUP_COOKIE, setup_token)
    r = await auth_client.post("/setup", data={"totp_code": "000000"})
    assert r.status_code == 200
    assert "Invalid" in r.text
    # Secret NOT persisted
    async with db.execute("SELECT value FROM auth_config WHERE key = 'totp_secret'") as cur:
        row = await cur.fetchone()
    assert row is None


async def test_setup_redirects_if_already_configured(
    auth_client: HttpxAsyncClient,
    db: aiosqlite.Connection,
) -> None:
    secret = pyotp.random_base32()
    await _insert_totp_secret(db, secret)
    r = await auth_client.get("/setup")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# --- logout ---

async def test_logout(authed_client: HttpxAsyncClient) -> None:
    r = await authed_client.post("/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    # Cookie is cleared (Max-Age=0 or deleted)
    assert r.cookies.get("session") in (None, "")
