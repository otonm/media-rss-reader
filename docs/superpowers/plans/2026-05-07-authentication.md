# Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add username/password + TOTP authentication with signed 7-day session cookies to the media RSS reader, which is exposed to the internet behind a TLS-terminating reverse proxy.

**Architecture:** A new `src/auth/` module handles session signing (`itsdangerous`), TOTP (`pyotp`), and IP-based lockout entirely in isolation. A Starlette middleware layer enforces HTTPS (via `X-Forwarded-Proto`) and session validity on every request. Routes at `/login`, `/setup`, and `/logout` handle credential submission and first-time TOTP setup. Static HTML pages for login and setup are standalone (no SPA dependency).

**Tech Stack:** `itsdangerous` (signed cookies), `pyotp` (TOTP), `node-qrcode` browser build (client-side QR rendering, no CDN), `pydantic SecretStr` (credential safety), `secrets.compare_digest` (timing-safe comparison), FastAPI `Form`, Starlette `BaseHTTPMiddleware`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Add `itsdangerous`, `pyotp` to dependencies |
| `src/db/migrations.py` | Modify | Append migration v4: `auth_config` table |
| `src/config.py` | Modify | Add 5 auth settings |
| `src/auth/__init__.py` | Create | Empty package marker |
| `src/auth/session.py` | Create | Sign/verify session and setup cookies |
| `src/auth/lockout.py` | Create | In-process IP lockout tracker |
| `src/auth/totp.py` | Create | Generate secrets, build URIs, verify codes |
| `src/auth/middleware.py` | Create | HTTPS enforcement + session check |
| `src/auth/routes.py` | Create | `/login`, `/setup`, `/logout` handlers |
| `src/static/login.html` | Create | Standalone login form |
| `src/static/setup.html` | Create | First-time TOTP setup page |
| `src/static/qrcode.min.js` | Create | node-qrcode browser build (bundled) |
| `src/main.py` | Modify | Register auth router + middleware |
| `tests/conftest.py` | Modify | Add auth env defaults + auth fixtures |
| `tests/test_auth.py` | Create | All auth tests |

---

## Task 1: Dependencies, DB Migration, Static Assets

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/db/migrations.py`
- Create: `src/static/qrcode.min.js`

- [ ] **Step 1: Add Python dependencies to pyproject.toml**

In `pyproject.toml`, add `itsdangerous` and `pyotp` to the `dependencies` list:

```toml
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "httpx",
    "feedparser",
    "listparser",
    "apscheduler",
    "aiosqlite",
    "pydantic-settings",
    "itsdangerous",
    "pyotp",
]
```

- [ ] **Step 2: Sync dependencies**

```bash
uv sync --extra dev
```

Expected: `itsdangerous` and `pyotp` appear in the resolved output. No errors.

- [ ] **Step 3: Append migration v4 to src/db/migrations.py**

Append one item to the `MIGRATIONS` list (never edit or reorder existing entries):

```python
MIGRATIONS: list[str] = [
    # v1: index on fetched_at to support age-based pruning queries
    "CREATE INDEX IF NOT EXISTS idx_items_fetched_at ON items(fetched_at)",
    # v2: seen_guids tombstone table — tracks seen state independently of pruning
    (
        "CREATE TABLE IF NOT EXISTS seen_guids ("
        "feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE, "
        "guid TEXT NOT NULL, "
        "seen_at TIMESTAMP NOT NULL, "
        "PRIMARY KEY (feed_id, guid))"
    ),
    # v3: backfill seen_guids from items that are already marked seen
    "INSERT OR IGNORE INTO seen_guids (feed_id, guid, seen_at) SELECT feed_id, guid, seen_at FROM items WHERE seen_at IS NOT NULL",
    # v4: auth_config table for storing TOTP secret
    "CREATE TABLE IF NOT EXISTS auth_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]
```

- [ ] **Step 4: Download node-qrcode browser build**

```bash
curl -L "https://unpkg.com/qrcode@1.5.4/build/qrcode.min.js" -o src/static/qrcode.min.js
```

Expected: `src/static/qrcode.min.js` exists and is ~45 KB. Verify:

```bash
ls -lh src/static/qrcode.min.js
head -c 80 src/static/qrcode.min.js
```

Expected first line starts with `!function(`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/db/migrations.py src/static/qrcode.min.js
git commit -m "feat(auth): add itsdangerous/pyotp deps, auth_config migration, qrcode.min.js"
```

---

## Task 2: Auth Config Settings

**Files:**
- Modify: `src/config.py`
- Modify: `tests/conftest.py` (add env defaults at top — required before any src.* imports)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (file already exists — append these test functions):

```python
def test_auth_settings_defaults() -> None:
    assert settings.auth_lockout_attempts == 5
    assert settings.auth_lockout_minutes == 15


def test_auth_settings_are_present() -> None:
    assert hasattr(settings, "auth_username")
    assert hasattr(settings, "auth_password")
    assert hasattr(settings, "auth_secret_key")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_config.py::test_auth_settings_defaults tests/test_config.py::test_auth_settings_are_present -v
```

Expected: `FAILED` — `AttributeError` or similar (settings fields don't exist yet).

- [ ] **Step 3: Add env defaults to conftest.py**

At the very top of `tests/conftest.py`, before all existing imports, add:

```python
import os

# Auth env vars must be set before src.config is imported (it instantiates Settings() at module level).
os.environ.setdefault("AUTH_USERNAME", "testuser")
os.environ.setdefault("AUTH_PASSWORD", "testpassword")
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-minimum-32-chars!!")
```

- [ ] **Step 4: Add auth settings to src/config.py**

```python
from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Paths ---
    opml_path: str = "/data/feeds.opml"
    db_path: str = "/data/db/reader.db"

    # --- Feed refresh schedule ---
    opml_sync_interval: int = 3600
    feed_refresh_interval: int = 900

    # --- Media cache ---
    cache_dir: str = "/cache"
    cache_max_items: int = 500
    cache_max_age_hours: int = 48

    # --- Item retention ---
    prefetch_ahead: int = 5
    keep_items: int = 1000
    items_max_age_hours: int = 168

    # --- Frontend behaviour ---
    image_display_delay_ms: int = 5000
    slideshow_transition_ms: int = 400
    auto_scroll_speed: float = 1.5

    # --- Server ---
    port: int = 8080
    log_level: str = "info"

    # --- Authentication ---
    auth_username: str
    auth_password: SecretStr
    auth_secret_key: SecretStr
    auth_lockout_attempts: int = 5
    auth_lockout_minutes: int = 15


settings = Settings()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all test_config.py tests pass including the two new ones.

- [ ] **Step 6: Commit**

```bash
git add src/config.py tests/conftest.py tests/test_config.py
git commit -m "feat(auth): add auth settings to config + test env defaults"
```

---

## Task 3: Session Cookie Signing (`src/auth/session.py`)

**Files:**
- Create: `src/auth/__init__.py`
- Create: `src/auth/session.py`
- Test: `tests/test_auth.py` (start with session tests)

- [ ] **Step 1: Create the package marker**

Create `src/auth/__init__.py` as an empty file.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_auth.py` with session tests:

```python
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
```

- [ ] **Step 3: Run to verify failure**

```bash
uv run pytest tests/test_auth.py::test_sign_and_verify_session -v
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'src.auth.session'`.

- [ ] **Step 4: Implement src/auth/session.py**

```python
"""Session cookie signing and verification.

Session cookies use itsdangerous.URLSafeTimedSerializer so each token
carries an embedded timestamp — no server-side store needed. The
setup cookie embeds the pending TOTP secret for the 10-minute window
between password auth and TOTP confirmation.
"""

import itsdangerous

SESSION_COOKIE = "session"
SETUP_COOKIE = "totp_setup"
SESSION_MAX_AGE = 604800  # 7 days in seconds
SETUP_MAX_AGE = 600  # 10 minutes in seconds

_SENTINEL = "authenticated"


def sign_session(secret_key: str) -> str:
    """Return a signed, timestamped session token."""
    return itsdangerous.URLSafeTimedSerializer(secret_key).dumps(_SENTINEL)


def verify_session(token: str, secret_key: str) -> bool:
    """Return True if the token is valid and within SESSION_MAX_AGE seconds."""
    try:
        itsdangerous.URLSafeTimedSerializer(secret_key).loads(token, max_age=SESSION_MAX_AGE)
        return True
    except itsdangerous.BadData:
        return False


def sign_setup_cookie(totp_secret: str, signing_key: str) -> str:
    """Embed the TOTP secret in a short-lived signed cookie payload."""
    return itsdangerous.URLSafeTimedSerializer(signing_key).dumps(totp_secret)


def verify_setup_cookie(token: str, signing_key: str) -> str | None:
    """Return the TOTP secret if the setup cookie is valid, else None."""
    try:
        return itsdangerous.URLSafeTimedSerializer(signing_key).loads(token, max_age=SETUP_MAX_AGE)
    except itsdangerous.BadData:
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_auth.py -v -k "session or setup_cookie or constants"
```

Expected: all 7 session tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/auth/__init__.py src/auth/session.py tests/test_auth.py
git commit -m "feat(auth): implement session cookie signing"
```

---

## Task 4: IP Lockout Tracker (`src/auth/lockout.py`)

**Files:**
- Create: `src/auth/lockout.py`
- Test: `tests/test_auth.py` (append lockout tests)

- [ ] **Step 1: Append failing tests to tests/test_auth.py**

```python
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
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_auth.py -v -k "lockout"
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'src.auth.lockout'`.

- [ ] **Step 3: Implement src/auth/lockout.py**

```python
"""In-process IP-based brute-force lockout.

Tracks failed login attempts per client IP using a monotonic clock so
it is immune to system clock changes. State is lost on process restart —
acceptable for a single-process deployment.
"""

import time
from dataclasses import dataclass, field


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = field(default=0.0)


class LockoutTracker:
    def __init__(self, max_attempts: int, lockout_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._entries: dict[str, _Entry] = {}

    def is_locked(self, ip: str) -> bool:
        """Return True if this IP is currently locked out."""
        entry = self._entries.get(ip)
        if entry is None:
            return False
        if entry.locked_until > time.monotonic():
            return True
        # Lockout window has elapsed — reset so failures don't accumulate forever.
        if entry.failures >= self._max_attempts:
            entry.failures = 0
            entry.locked_until = 0.0
        return False

    def record_failure(self, ip: str) -> None:
        """Increment the failure counter and lock if threshold is reached."""
        entry = self._entries.setdefault(ip, _Entry())
        entry.failures += 1
        if entry.failures >= self._max_attempts:
            entry.locked_until = time.monotonic() + self._lockout_seconds

    def reset(self, ip: str) -> None:
        """Clear all failure state for this IP (call on successful login)."""
        self._entries.pop(ip, None)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_auth.py -v -k "lockout"
```

Expected: all 5 lockout tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/auth/lockout.py tests/test_auth.py
git commit -m "feat(auth): implement IP lockout tracker"
```

---

## Task 5: TOTP Utilities (`src/auth/totp.py`)

**Files:**
- Create: `src/auth/totp.py`
- Test: `tests/test_auth.py` (append TOTP tests)

- [ ] **Step 1: Append failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_auth.py -v -k "totp or generate_secret or build_uri or verify_code"
```

Expected: `FAILED` — `ModuleNotFoundError`.

- [ ] **Step 3: Implement src/auth/totp.py**

```python
"""TOTP utilities: generate secrets, build provisioning URIs, verify codes."""

import pyotp


def generate_secret() -> str:
    """Generate a cryptographically random base32 TOTP secret."""
    return pyotp.random_base32()


def build_uri(secret: str, username: str, issuer: str = "MediaRSSReader") -> str:
    """Return an otpauth:// URI suitable for QR code generation."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_code(secret: str, code: str) -> bool:
    """Return True if code is valid for the current ±1 time step (30 s window)."""
    if not code:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_auth.py -v -k "totp or generate_secret or build_uri or verify_code"
```

Expected: all 6 TOTP tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/auth/totp.py tests/test_auth.py
git commit -m "feat(auth): implement TOTP utilities"
```

---

## Task 6: HTML Templates

**Files:**
- Create: `src/static/login.html`
- Create: `src/static/setup.html`

These are standalone pages (not the SPA) that include inline CSS matching the app's dark theme. They use no external resources except `/static/qrcode.min.js` (setup page only), which is already bundled.

- [ ] **Step 1: Create src/static/login.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in — Media RSS Reader</title>
  <style>
    html, body { background: #111; color: #eee; font-family: sans-serif; margin: 0; }
    body { display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    form { display: flex; flex-direction: column; gap: 0.75rem; width: 100%; max-width: 300px; padding: 1rem; }
    h1 { text-align: center; font-size: 1.1rem; margin: 0 0 0.5rem; }
    label { font-size: 0.8rem; color: #aaa; }
    input {
      background: #1e1e1e; border: 1px solid #444; color: #eee;
      padding: 0.6rem 0.75rem; border-radius: 4px; font-size: 1rem; width: 100%; box-sizing: border-box;
    }
    input:focus { outline: none; border-color: #777; }
    button {
      background: #2a2a2a; color: #eee; border: 1px solid #555;
      padding: 0.65rem; border-radius: 4px; cursor: pointer; font-size: 1rem; margin-top: 0.25rem;
    }
    button:hover { background: #333; }
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Media RSS Reader</h1>
    <label for="username">Username</label>
    <input id="username" type="text" name="username" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <input id="password" type="password" name="password" autocomplete="current-password" required>
    <label for="totp_code">Authenticator code <span style="color:#666">(leave empty on first login)</span></label>
    <input id="totp_code" type="text" name="totp_code" inputmode="numeric"
           pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code">
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
```

- [ ] **Step 2: Create src/static/setup.html**

The server replaces `{{TOTP_URI}}` (HTML-escaped), `{{TOTP_SECRET}}`, and `{{ERROR}}` before serving. The `data-totp-uri` attribute carries the URI to JS; the browser automatically unescapes HTML entities when reading `dataset.totpUri`.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Set up authenticator — Media RSS Reader</title>
  <style>
    html, body { background: #111; color: #eee; font-family: sans-serif; margin: 0; }
    body { display: flex; justify-content: center; align-items: flex-start; min-height: 100vh; padding: 2rem 1rem; box-sizing: border-box; }
    .container { display: flex; flex-direction: column; gap: 1rem; width: 100%; max-width: 360px; }
    h1 { font-size: 1.1rem; margin: 0; text-align: center; }
    p { font-size: 0.85rem; color: #aaa; margin: 0; }
    .qr-wrap { display: flex; justify-content: center; background: #fff; padding: 1rem; border-radius: 6px; }
    code {
      display: block; font-size: 0.85rem; background: #1e1e1e; border: 1px solid #333;
      padding: 0.5rem 0.75rem; border-radius: 4px; letter-spacing: 0.06em;
      word-break: break-all; cursor: pointer; user-select: all;
    }
    code:hover { background: #252525; }
    label { font-size: 0.8rem; color: #aaa; }
    input {
      background: #1e1e1e; border: 1px solid #444; color: #eee;
      padding: 0.6rem 0.75rem; border-radius: 4px; font-size: 1rem; width: 100%; box-sizing: border-box;
    }
    input:focus { outline: none; border-color: #777; }
    button {
      background: #2a2a2a; color: #eee; border: 1px solid #555;
      padding: 0.65rem; border-radius: 4px; cursor: pointer; font-size: 1rem;
    }
    button:hover { background: #333; }
    .error { color: #f88; font-size: 0.85rem; min-height: 1.2em; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Set up authenticator</h1>
    <p>Scan this QR code with your authenticator app (Google Authenticator, Aegis, etc.):</p>
    <div class="qr-wrap">
      <canvas id="qrcode" data-totp-uri="{{TOTP_URI}}"></canvas>
    </div>
    <p>Or copy this key into your app manually:</p>
    <code id="secret" title="Click to copy" onclick="navigator.clipboard.writeText(this.textContent)">{{TOTP_SECRET}}</code>
    <form method="post" action="/setup" style="display:flex;flex-direction:column;gap:0.75rem;">
      <label for="totp_code">Enter the 6-digit code from your app to confirm:</label>
      <input id="totp_code" type="text" name="totp_code" inputmode="numeric"
             pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" required autofocus>
      <div class="error">{{ERROR}}</div>
      <button type="submit">Confirm and sign in</button>
    </form>
  </div>
  <script src="/static/qrcode.min.js"></script>
  <script>
    (function () {
      var canvas = document.getElementById('qrcode');
      QRCode.toCanvas(canvas, canvas.dataset.totpUri, { width: 200 }, function (err) {
        if (err) { console.error('QR render error:', err); }
      });
    })();
  </script>
</body>
</html>
```

- [ ] **Step 3: Commit**

```bash
git add src/static/login.html src/static/setup.html
git commit -m "feat(auth): add login and setup HTML templates"
```

---

## Task 7: Routes + conftest Fixtures + Integration Tests

**Files:**
- Create: `src/auth/routes.py`
- Modify: `tests/conftest.py` (add auth fixtures)
- Test: `tests/test_auth.py` (append route tests)

- [ ] **Step 1: Add auth fixtures to tests/conftest.py**

Append to `tests/conftest.py` (after the existing `client` fixture):

```python
import src.auth.routes as _auth_routes
from src.auth.middleware import AuthMiddleware
from src.auth.session import SESSION_COOKIE, sign_session
from src.config import settings


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override settings attributes for auth tests. Resets after each test."""
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "auth_username", "admin")
    monkeypatch.setattr(settings, "auth_password", SecretStr("hunter2"))
    monkeypatch.setattr(settings, "auth_secret_key", SecretStr("test-secret-key-minimum-32-chars!!"))
    monkeypatch.setattr(settings, "auth_lockout_attempts", 5)
    monkeypatch.setattr(settings, "auth_lockout_minutes", 15)


@pytest.fixture
async def auth_client(
    db: aiosqlite.Connection,
    auth_settings: None,
) -> AsyncGenerator[HttpxAsyncClient]:
    """Test client with auth routes + middleware. Resets lockout state each test."""
    from src.auth.lockout import LockoutTracker

    # Re-create module-level lockout so failures from one test don't bleed into the next.
    _auth_routes._lockout = LockoutTracker(
        max_attempts=settings.auth_lockout_attempts,
        lockout_seconds=settings.auth_lockout_minutes * 60,
    )

    test_app = FastAPI()
    test_app.add_middleware(AuthMiddleware)
    test_app.include_router(_auth_routes.router)

    @test_app.get("/")
    async def _root() -> dict[str, str]:
        return {"status": "ok"}

    async def _override_db() -> AsyncIterator[aiosqlite.Connection]:
        yield db

    test_app.dependency_overrides[get_db] = _override_db

    async with HttpxAsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    ) as c:
        yield c


@pytest.fixture
async def authed_client(auth_client: HttpxAsyncClient) -> HttpxAsyncClient:
    """auth_client pre-loaded with a valid session cookie."""
    token = sign_session(settings.auth_secret_key.get_secret_value())
    auth_client.cookies.set(SESSION_COOKIE, token)
    return auth_client
```

- [ ] **Step 2: Append route tests to tests/test_auth.py**

```python
# ---------------------------------------------------------------------------
# Route integration tests (require auth_client / authed_client fixtures)
# ---------------------------------------------------------------------------

import pyotp
from httpx import AsyncClient as HttpxAsyncClient

from src.auth.session import SETUP_COOKIE, sign_setup_cookie
from src.config import settings


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
```

- [ ] **Step 3: Run to verify failure**

```bash
uv run pytest tests/test_auth.py -v -k "login or setup or logout"
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'src.auth.routes'`.

- [ ] **Step 4: Implement src/auth/routes.py**

```python
"""Authentication routes: /login, /setup, /logout."""

import html as _html
import secrets
from pathlib import Path
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, Form, Request
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

_lockout = LockoutTracker(
    max_attempts=settings.auth_lockout_attempts,
    lockout_seconds=settings.auth_lockout_minutes * 60,
)

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or "unknown"


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
    return (_static / "login.html").read_text()


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
        (_static / "setup.html")
        .read_text()
        .replace("{{TOTP_URI}}", _html.escape(uri))
        .replace("{{TOTP_SECRET}}", secret)
        .replace("{{ERROR}}", "")
    )
    return HTMLResponse(html)


@router.post("/setup")
async def post_setup(
    request: Request,
    totp_code: str = Form(...),
    db: _DbDep = None,  # type: ignore[assignment]
) -> Response:
    if await _load_totp_secret(db) is not None:
        return RedirectResponse("/login", status_code=302)

    setup_token = request.cookies.get(SETUP_COOKIE, "")
    secret = verify_setup_cookie(setup_token, settings.auth_secret_key.get_secret_value())
    if secret is None:
        return Response("Setup session expired. Please log in again.", status_code=403)

    if not totp_module.verify_code(secret, totp_code):
        uri = totp_module.build_uri(secret, settings.auth_username)
        html = (
            (_static / "setup.html")
            .read_text()
            .replace("{{TOTP_URI}}", _html.escape(uri))
            .replace("{{TOTP_SECRET}}", secret)
            .replace("{{ERROR}}", "Invalid code. Try again.")
        )
        resp = HTMLResponse(html)
        _set_setup_cookie(resp, secret)
        return resp

    await db.execute(
        "INSERT OR REPLACE INTO auth_config (key, value) VALUES ('totp_secret', ?)", (secret,)
    )
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
```

- [ ] **Step 5: Run route tests to verify they pass**

```bash
uv run pytest tests/test_auth.py -v -k "login or setup or logout"
```

Expected: all route tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/auth/routes.py tests/conftest.py tests/test_auth.py
git commit -m "feat(auth): implement auth routes and integration tests"
```

---

## Task 8: Auth Middleware (`src/auth/middleware.py`)

**Files:**
- Create: `src/auth/middleware.py`
- Test: `tests/test_auth.py` (append middleware tests)

- [ ] **Step 1: Append middleware tests to tests/test_auth.py**

```python
# ---------------------------------------------------------------------------
# middleware tests
# ---------------------------------------------------------------------------

from httpx import ASGITransport
from httpx import AsyncClient as HttpxAsyncClient
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.auth.middleware import AuthMiddleware


async def _make_plain_app() -> FastAPI:
    """Minimal app with AuthMiddleware and a single protected route."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/")
    async def _root() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login")
    async def _login() -> dict[str, str]:
        return {"page": "login"}

    return app


async def test_https_enforcement() -> None:
    app = await _make_plain_app()
    async with HttpxAsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        r = await c.get("/")
    assert r.status_code == 403


async def test_login_page_bypasses_auth() -> None:
    app = await _make_plain_app()
    async with HttpxAsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    ) as c:
        r = await c.get("/login")
    assert r.status_code == 200


async def test_protected_route_no_cookie() -> None:
    app = await _make_plain_app()
    async with HttpxAsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    ) as c:
        r = await c.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_protected_route_valid_cookie(auth_settings: None) -> None:
    app = await _make_plain_app()
    token = sign_session(settings.auth_secret_key.get_secret_value())
    async with HttpxAsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
        cookies={SESSION_COOKIE: token},
    ) as c:
        r = await c.get("/")
    assert r.status_code == 200


async def test_protected_route_tampered_cookie() -> None:
    app = await _make_plain_app()
    async with HttpxAsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
        cookies={SESSION_COOKIE: "totally.fake.token"},
    ) as c:
        r = await c.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_auth.py -v -k "https or bypass or protected or tampered"
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'src.auth.middleware'`.

- [ ] **Step 3: Implement src/auth/middleware.py**

```python
"""Starlette middleware: HTTPS enforcement and session validation.

Passes through /login, /setup, and all /static/* paths so the login
and setup pages (including their assets) are accessible without auth.
All other paths require a valid signed session cookie.
"""

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
```

- [ ] **Step 4: Run middleware tests to verify they pass**

```bash
uv run pytest tests/test_auth.py -v -k "https or bypass or protected or tampered"
```

Expected: all 5 middleware tests pass.

- [ ] **Step 5: Run the full test_auth.py**

```bash
uv run pytest tests/test_auth.py -v
```

Expected: all tests in test_auth.py pass.

- [ ] **Step 6: Commit**

```bash
git add src/auth/middleware.py tests/test_auth.py
git commit -m "feat(auth): implement HTTPS enforcement + session middleware"
```

---

## Task 9: Wire Up main.py

**Files:**
- Modify: `src/main.py`

- [ ] **Step 1: Register the auth router and middleware in src/main.py**

The final `src/main.py` (full file — show the complete content):

```python
"""FastAPI application entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api import feeds, items, media
from src.auth.middleware import AuthMiddleware
from src.auth import routes as auth_routes
from src.config import settings
from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema
from src.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=settings.log_level.upper())
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_static_dir = Path(__file__).parent / "static"
_index_path = _static_dir / "index.html"


def _build_html() -> str:
    if not _index_path.exists():
        return ""
    style = (
        f"<style>:root{{"
        f"--slideshow-transition-ms:{settings.slideshow_transition_ms}ms;"
        f"--image-display-delay-ms:{settings.image_display_delay_ms}ms;"
        f"--prefetch-ahead:{settings.prefetch_ahead};"
        f"--auto-scroll-speed:{settings.auto_scroll_speed}"
        f"}}</style>"
    )
    return _index_path.read_text().replace("<!-- SLIDESHOW_TRANSITION -->", style)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.html = _build_html()
    db = await open_db(settings.db_path)
    await create_schema(db)
    await run_migrations(db)
    app.state.db = db
    await start_scheduler(db)
    yield
    await stop_scheduler()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(auth_routes.router)
app.include_router(feeds.router, prefix="/api")
app.include_router(items.router, prefix="/api")
app.include_router(media.router, prefix="/api")

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    return request.app.state.html
```

- [ ] **Step 2: Run all tests to verify nothing is broken**

```bash
uv run pytest -v
```

Expected: all tests pass (including pre-existing tests). Coverage ≥ 90%.

If existing tests fail because the test client no longer sends `X-Forwarded-Proto: https`, update the `client` fixture in `conftest.py` to add `headers={"x-forwarded-proto": "https"}`:

```python
@pytest.fixture
async def client(db: aiosqlite.Connection) -> AsyncGenerator[HttpxAsyncClient]:
    test_app = FastAPI()
    test_app.include_router(feeds_router.router, prefix="/api")
    test_app.include_router(items_router.router, prefix="/api")
    test_app.include_router(media_router.router, prefix="/api")

    async def _override_db() -> AsyncIterator[aiosqlite.Connection]:
        yield db

    test_app.dependency_overrides[get_db] = _override_db

    async with HttpxAsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
        headers={"x-forwarded-proto": "https"},
    ) as c:
        yield c
```

Note: the existing `client` fixture does NOT include the auth middleware, so existing API tests should pass regardless of the header. The `X-Forwarded-Proto` header is only checked by `AuthMiddleware`, which is not in the `client` fixture's app.

- [ ] **Step 3: Run linter**

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: no errors. If any, fix with `uv run ruff check --fix . && uv run ruff format .` then re-run.

- [ ] **Step 4: Commit**

```bash
git add src/main.py tests/conftest.py
git commit -m "feat(auth): wire auth middleware and routes into main app"
```

---

## Task 10: Final Verification

- [ ] **Step 1: Run full test suite with coverage**

```bash
uv run pytest -v
```

Expected: all tests pass, coverage ≥ 90%.

- [ ] **Step 2: Smoke-test the running app (manual)**

```bash
AUTH_USERNAME=admin AUTH_PASSWORD=secret AUTH_SECRET_KEY=a-random-32-char-secret-key-here \
  uv run uvicorn src.main:app --reload --port 8080 \
  --forwarded-allow-ips='*'
```

Then open `http://localhost:8080`. You will be redirected to `/login`.

Note: since the middleware requires `X-Forwarded-Proto: https`, make the test request with:
```bash
curl -v -H "X-Forwarded-Proto: https" http://localhost:8080/
```

Expected: `302` redirect to `/login`.

```bash
curl -v -H "X-Forwarded-Proto: https" http://localhost:8080/login
```

Expected: `200` with the login form HTML.

- [ ] **Step 3: Update docker-compose.yml with required env vars**

Add the three required auth env vars to `docker-compose.yml` (as commented-out placeholders so the user fills them in):

```yaml
environment:
  - TZ=Europe/Berlin
  # - LOG_LEVEL=info
  - AUTH_USERNAME=admin
  - AUTH_PASSWORD=changeme
  - AUTH_SECRET_KEY=replace-with-a-random-32-char-secret
```

- [ ] **Step 4: Final commit**

```bash
git add docker-compose.yml
git commit -m "feat(auth): add required auth env vars to docker-compose.yml"
```

---

## Verification End-to-End

1. **Unit tests:** `uv run pytest -v` — all pass, coverage ≥ 90%
2. **Linter:** `uv run ruff check . && uv run ruff format --check .` — clean
3. **First login flow:** Start app → visit `/` → redirected to `/login` → enter username + password (no TOTP) → redirected to `/setup` → scan QR or copy code → enter TOTP code → redirected to `/` → app works normally
4. **Normal login:** Visit `/login` → enter all three fields → session cookie set → works for 7 days
5. **Lockout:** 5 wrong attempts → 429 on 6th → wait 15 min or restart process
6. **Logout:** A `POST /logout` from the UI → cookie cleared → redirected to `/login`
7. **Kill switch:** Change `AUTH_SECRET_KEY` env var → restart container → all sessions invalidated
