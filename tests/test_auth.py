"""Authentication tests."""

import time

import pytest

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
