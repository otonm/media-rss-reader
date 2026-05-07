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
