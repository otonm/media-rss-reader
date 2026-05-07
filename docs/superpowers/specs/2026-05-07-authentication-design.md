# Authentication Design

**Date:** 2026-05-07
**Status:** Approved

## Context

The media RSS reader is exposed to the internet and currently has no authentication. This design adds single-user authentication using username/password (configured via env vars) plus TOTP (generated on first login), with a one-week remembered session. The session is stateless — signed cookies via `itsdangerous` — so no server-side session store is needed.

---

## Architecture

A new `src/auth/` module is self-contained and isolated from existing app logic. Two new static HTML pages handle login and TOTP setup. One DB migration adds an `auth_config` table for the TOTP secret.

```
src/auth/
├── __init__.py
├── session.py       # sign/verify cookies with itsdangerous.URLSafeTimedSerializer
├── totp.py          # generate base32 secret, build otpauth:// URI, verify codes
├── lockout.py       # in-process IP lockout tracker (dict in memory)
├── middleware.py    # Starlette middleware: enforces HTTPS + valid session on all routes
└── routes.py        # GET/POST /login, GET/POST /setup, POST /logout

src/static/
├── login.html       # login form (username + password + TOTP)
├── setup.html       # first-time TOTP setup (QR + copyable secret + confirm field)
└── qrcode.min.js    # bundled QR library — no CDN dependency
```

`main.py` changes:
- Register `auth.routes.router`
- Add `AuthMiddleware` to the app

`src/db/migrations.py` change:
- Append migration v4: `CREATE TABLE IF NOT EXISTS auth_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)`

---

## Security Model

| Concern | Decision |
|---|---|
| **HTTPS enforcement** | Middleware rejects requests where `X-Forwarded-Proto != https` with `403`. Proxy is always trusted. |
| **Session cookie** | `HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=604800` (7 days). Signed + timestamped by `itsdangerous`. |
| **Kill switch** | Rotating `AUTH_SECRET_KEY` invalidates all sessions instantly. |
| **Password comparison** | `secrets.compare_digest` — constant-time. Password held as `SecretStr`; `.get_secret_value()` only called at comparison time. |
| **TOTP window** | `pyotp` with `valid_window=1` — accepts previous/current/next 30s slot (±30s clock skew tolerance). |
| **Brute-force lockout** | In-process dict keyed by `X-Forwarded-For` first IP. 5 failures → locked for 15 minutes. Counter resets on successful login. |
| **CSRF** | Not implemented. Login CSRF is moot for single-user (no benefit to attacker). Logout worst case is logging the user out — acceptable. |
| **TOTP secret storage** | Stored in SQLite `auth_config` table as `('totp_secret', '<base32>')`. |
| **QR code rendering** | Client-side via bundled [`node-qrcode`](https://github.com/soldair/node-qrcode) browser build (`qrcode.min.js`, ~45KB) from the `otpauth://` URI — no CDN, no server-side image generation, no extra Python deps. |
| **Lockout IP source** | `X-Forwarded-For` first value. Assumption: reverse proxy sets this and clients cannot spoof it. |

---

## Auth Flows

### Normal login (TOTP already configured)

```
GET  /login   → serve login.html
POST /login   → check IP lockout (429 if locked)
              → verify username + password via secrets.compare_digest (401 on fail, increment counter)
              → verify TOTP code via pyotp (401 on fail, increment counter)
              → reset lockout counter
              → set signed session cookie (7-day Max-Age)
              → 303 redirect → /
```

### First login (no TOTP secret in DB)

```
POST /login   → check lockout → verify username + password
              → detect missing TOTP secret
              → generate base32 secret
              → generate base32 secret, embed it in a short-lived signed "setup cookie"
                (cookie payload = base32 secret string; 10-min TTL via itsdangerous max_age)
              → 303 redirect → /setup

GET  /setup   → verify setup cookie (403 if missing/expired)
              → if TOTP already configured in DB → 302 redirect → /login (setup not repeatable)
              → serve setup.html with:
                  - otpauth:// URI (qrcode.min.js renders QR client-side)
                  - base32 secret as copyable text
POST /setup   → verify setup cookie
              → verify submitted TOTP code against temporary secret
              → on success: persist secret to auth_config table
                            clear setup cookie, set full 7-day session cookie
                            303 redirect → /
              → on failure: re-render setup.html with error (secret unchanged)
```

### Every protected request

```
AuthMiddleware passes through: /login, /setup, /static/login.html,
                               /static/setup.html, /static/qrcode.min.js
All other routes: verify signed session cookie
                  → valid   → proceed
                  → invalid/expired → 302 redirect → /login
```

### Logout

```
POST /logout  → delete session cookie (Max-Age=0) → 302 redirect → /login
```

---

## Configuration

New env vars added to `src/config.py` (`Settings` class):

| Variable | Type | Default | Description |
|---|---|---|---|
| `AUTH_USERNAME` | `str` | required | Login username |
| `AUTH_PASSWORD` | `SecretStr` | required | Login password |
| `AUTH_SECRET_KEY` | `SecretStr` | required | Signs session cookies; rotate to invalidate all sessions |
| `AUTH_LOCKOUT_ATTEMPTS` | `int` | `5` | Failed attempts before lockout |
| `AUTH_LOCKOUT_MINUTES` | `int` | `15` | Lockout duration in minutes |

All three required fields have no default — pydantic-settings raises on startup if missing.

---

## Data Model

Migration v4 appended to `src/db/migrations.py`:

```sql
CREATE TABLE IF NOT EXISTS auth_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
```

Single row written on first successful TOTP setup: `('totp_secret', '<base32>')`.

---

## New Dependencies

| Package | Use |
|---|---|
| `itsdangerous` | Signs and verifies session and setup cookies |
| `pyotp` | Generates TOTP secrets, builds `otpauth://` URIs, verifies codes |

No new Python deps for QR rendering — handled by bundled `qrcode.min.js` (node-qrcode browser build, downloaded from the project's GitHub releases and committed to the repo).

---

## Testing

New file `tests/test_auth.py`:

| Test | What it verifies |
|---|---|
| `test_login_success` | Valid credentials + TOTP → 303 redirect, session cookie set |
| `test_login_wrong_password` | Bad password → 401, no cookie |
| `test_login_wrong_totp` | Correct password, bad TOTP → 401, no cookie |
| `test_login_lockout` | 5 failures → subsequent attempt returns 429 even with correct credentials |
| `test_lockout_resets_on_success` | Successful login clears the failure counter |
| `test_protected_route_no_cookie` | `GET /` without cookie → 302 to `/login` |
| `test_protected_route_valid_cookie` | `GET /` with valid signed cookie → 200 |
| `test_protected_route_expired_cookie` | Tampered/expired cookie → 302 to `/login` |
| `test_setup_flow_first_login` | No TOTP in DB → login redirects to `/setup` |
| `test_setup_confirms_totp` | Valid TOTP on `/setup` → secret persisted, session set |
| `test_setup_wrong_totp` | Bad TOTP on `/setup` → error, secret not persisted |
| `test_logout` | `POST /logout` → cookie cleared, redirect to `/login` |
| `test_https_enforcement` | Request without `X-Forwarded-Proto: https` → 403 |
| `test_login_page_bypasses_auth` | `/login` accessible without session cookie |

`conftest.py` additions:
- `auth_settings` fixture — injects test credentials and a fixed secret key
- `authed_client` fixture — pre-sets a valid session cookie for authenticated tests

---

## Files Modified / Created

| File | Change |
|---|---|
| `src/auth/__init__.py` | New (empty) |
| `src/auth/session.py` | New |
| `src/auth/totp.py` | New |
| `src/auth/lockout.py` | New |
| `src/auth/middleware.py` | New |
| `src/auth/routes.py` | New |
| `src/static/login.html` | New |
| `src/static/setup.html` | New |
| `src/static/qrcode.min.js` | New (bundled) |
| `src/config.py` | Add 5 new settings |
| `src/main.py` | Register router + middleware |
| `src/db/migrations.py` | Append migration v4 |
| `tests/test_auth.py` | New |
| `tests/conftest.py` | Add `auth_settings` + `authed_client` fixtures |
| `pyproject.toml` | Add `itsdangerous`, `pyotp` dependencies |
