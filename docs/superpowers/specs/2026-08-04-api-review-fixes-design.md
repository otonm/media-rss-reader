# src/api review fixes — design spec

Source: `.claude-review/20260804-193601/REVIEW.md` (21 findings: 1 blocker, 7 major, 11 minor, 2 nit).
Scope: all 21 findings, cross-module (the review was scoped to `src/api/` but several fixes live in `src/config.py`, `src/auth/`, `src/media/fetch.py`, `src/media/availability.py`, `src/db/connection.py`).

## Goal

Close every finding from the 2026-08-04 deep review of `src/api`. The single BLOCKER (open proxy + forgeable default auth) must ship broken-fixed; the 7 MAJORs (input-validation correctness, unjustified `# type: ignore` cargo, DNS-rebinding SSRF, a mislabelled mid-stream abort log, three test gaps in `mark_seen` and the cursor-rank derivation) are fixed in the same pass; the 11 MINORs (untyped return shapes, dead `rn` column, observability gaps, cache-name duplication, `os.stat` vs `path.stat`) and 2 NITs (count-field and size-boundary tests) are also included.

## Architecture

The fixes are organized into six end-to-end themes, each a self-contained PR that closes a concern fully. Themes 1 + 2 together close the BLOCKER; 3–6 follow in any order (3 before 6 helps the prefetch test). A finding maps to exactly one theme.

The only net-new runtime component is a request-id middleware (theme 4); everything else is additive logging, validation gates, and typing cleanup. No new dependencies are introduced — pydantic (via FastAPI), httpx, aiosqlite, itsdangerous are already declared.

## Tech stack

Python 3.14 (`requires-python = ">=3.14"`), FastAPI + pydantic v2, aiosqlite, httpx, itsdangerous. Ruff (`select = E,W,F,I,UP,B,SIM,ANN,ASYNC`), pytest + pytest-asyncio (asyncio_mode=auto) + respx.

## Per-theme design

### Theme 1 — Auth hardening
**Files:** `src/config.py`, `tests/test_config.py`, `tests/test_auth.py`

**Empty-credential fail-fast at startup.** In `_load_settings()` (`src/config.py:78-88`), after constructing `Settings`, validate:
- `auth_secret_key == ""` → `raise RuntimeError("AUTH_SECRET_KEY must be set; the session signer must not be empty")`. The session signer must always be set, including the "no auth" mode (both username + password empty) — an empty key produces a forgeable `itsdangerous.URLSafeTimedSerializer`.
- exactly one of `auth_username` / `auth_password` empty → `raise RuntimeError("AUTH_USERNAME and AUTH_PASSWORD must both be set, or both empty (no-auth mode)")`. Both empty is allowed (no-auth mode) but still requires `AUTH_SECRET_KEY`.

This closes the BLOCKER's auth side: an out-of-the-box deploy can no longer serve a forgeable session.

**TDD:**
- `test_config.py` (new): empty `AUTH_SECRET_KEY` → `RuntimeError`; set `AUTH_SECRET_KEY` → no raise; username set + password empty → `RuntimeError`; both empty + key set → no raise.
- `test_auth.py`: empty creds + set key → `/login` returns 401 (login fails). The "empty key" case is unreachable because startup already raised (covered by config test).

The `_DbDep` cargo in `src/auth/routes.py` is moved to theme 3 so theme 1 only touches config validation.

### Theme 2 — Proxy SSRF + DNS pinning
**Files:** `src/api/media.py`, `src/api/reddit_feeds.py`, `src/media/availability.py`, `src/media/fetch.py`, `tests/test_api.py`, `tests/test_fetch.py`

**url-lookup gate (the BLOCKER's proxy side).** Add `is_known_media_url(url: str, db: aiosqlite.Connection) -> bool` in `src/media/availability.py` (it already owns `_item_urls`). Two-tier:
1. `SELECT 1 FROM items WHERE media_url = ? LIMIT 1` — uses the existing `idx_items_media_url` index (migration v9). Covers single-media items and the primary URL of a gallery.
2. If miss: `SELECT media_json FROM items WHERE media_json LIKE ?` with bind `f'%"{url}"%'` (narrow prefilter, no index) — for each row, `url in _item_urls(row)` membership in Python. Covers gallery slide URLs that live only in `media_json`.

`proxy_media` (`src/api/media.py:24-90`) gains a `db: _DbDep` dependency (it currently has none) and calls `is_known_media_url(url, db)` before `cache_read`/`open_upstream`; on miss → `raise HTTPException(status_code=404, detail="not a known media url")`.

**DNS IP pinning (MAJOR).** In `open_upstream` (`src/media/fetch.py`), after `_check_url(target)` resolves and validates, capture the validated IP. Build the request with the IP in the URL (`{scheme}://{ip}{path}`) and set `Host: {host}` in headers. For HTTPS, supply an `ssl_context` with `server_hostname=host` so TLS verifies against the original host. `_check_url` returns the validated addrs; `open_upstream` consumes the first. Re-run on each redirect hop (the loop at `fetch.py:130-144` already re-runs `_check_url`).

**`tee_to_cache` mid-stream log + mislabel fix (MAJOR, OBSERVABILITY).** In `tee_to_cache`'s `finally` (`fetch.py:222-230`), distinguish "client disconnect" from "server abort" via a flag set when `UpstreamError` is raised (line 216). Log server-abort at WARNING with `url`, `sent`, `settings.media_max_bytes`; log client-disconnect at DEBUG (current message). Wrap the `async for` so any other exception (e.g. disk-full from `cache_stream_tee`) logs at WARNING before re-raising.

**reddit_feeds hardening (MINOR).** `follow_redirects=False` (the companion service is on a fixed URL; surface 3xx as 502 by treating `resp.is_success` False the same as today) — `src/api/reddit_feeds.py:22`. Add `X-Content-Type-Options: nosniff` to the success `Response` (reddit_feeds.py:49).

**Enable ruff `S` (bandit).** Add `"S"` to `[tool.ruff.lint] select` in `pyproject.toml` so the open-SSRF and hardcoded-empty-credential classes are caught going forward. `# noqa` the legitimate `S` findings that surface (e.g. test fixtures using hardcoded passwords). One-line config change at the end of theme 2, since it directly prevents both BLOCKER classes from recurring.

**TDD:**
- `test_api.py`: proxy returns 404 for an unknown url; the 18 existing proxy tests (`tests/test_api.py:274,299,318,343,374,397,423,448,490,525,913,936,961,1061,1085` etc.) are updated to insert the url into `items.media_url` (or `media_json`) before the request; a gallery slide url (only in `media_json`) is served.
- `test_fetch.py`: DNS-rebinding case — a host whose first resolution is public and second is private → request is refused (respx + a fake resolver that returns different addrs per call). `tee_to_cache` server-abort logs WARNING; client-disconnect logs DEBUG.

### Theme 3 — Input validation & typing
**Files:** `src/api/media.py`, `src/api/items.py`, `src/api/feeds.py`, `src/db/connection.py`, `src/auth/routes.py`, `tests/test_api.py`

- **`PrefetchHint` BaseModel (MAJOR).** `class PrefetchHint(BaseModel): item_id: str; unseen: bool = True`. `async def prefetch_hint(body: PrefetchHint, db: _DbDep) -> dict[str, str]`. FastAPI validates; `bool("false")`-style coercion and `str(None)` item_id bypassing the 422 are gone. `test_prefetch_hint` sends `{"item_id": "x", "unseen": "false"}` and asserts 422.
- **TypedDicts (MINOR).** `ItemOut`, `FeedOut`, `SeenResponse` (`{"seen_at": str}`), `PrefetchHintResponse` (`{"status": str}`). Used as return annotations only — **no `response_model=`** (frontend impact: none, no validation strips fields).
- **Drop `rn`** from the outer SELECT (`items.py:107`); keep in the CTE and the WHERE clause.
- **`params: list[str | int]`** (`items.py:91`); drop `from typing import Any` if now unused.
- **`path.stat()`** (`media.py:57`) replacing `os.stat(path)`; drop `import os`.
- **Hoist `_DbDep`** to `src/db/connection.py` using py314's `type` statement: `type _DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]`. Import in `items.py`, `media.py`, `feeds.py`, `auth/routes.py`. Drop all four local declarations.
- **Drop `= None  # type: ignore[assignment]`** on all six sites (`items.py:56,129`; `media.py:96`; `auth/routes.py:41,92,157`). FastAPI injects via the `Depends()` marker in `Annotated` regardless of a default.

**TDD:** existing suite must stay green. Add one parametrize test for `PrefetchHint` rejecting `"false"` / `null` / non-string `item_id` with 422.

### Theme 4 — Observability
**Files:** new `src/request_id.py` (+ wiring in `src/main.py`), `src/api/media.py`, `src/api/items.py`, `src/api/feeds.py`, `src/api/reddit_feeds.py`, `src/media/fetch.py` (log lines only), `tests/test_request_id.py`, `tests/test_api.py`

- **Correlation id.** `src/request_id.py` — a `contextvars.ContextVar[str]` for `request_id`, a `RequestIDMiddleware` that sets it per request (UUID4 hex) and sets the `X-Request-ID` response header, and a `current_request_id() -> str | None` accessor. Route log lines include it via `extra={"request_id": current_request_id()}`. `open_upstream`/`tee_to_cache` accept an optional `request_id: str | None` param threaded into their log lines. Wired in `src/main.py`'s middleware stack.
- **DB-duration logging.** Bracket each `db.execute` in `src/api/` with `time.perf_counter()`; log elapsed ms on the existing outcome line (`items.py:114,144,153`, `feeds.py:23`, `media.py:110`). Keep inline (three lines per site — YAGNI for a helper used 5×).
- **`cache_present_names` boundary log** (`items.py:116`): log before `await asyncio.to_thread(...)`, after with `len(names)` + elapsed.
- **`proxy_media` success exit log** (`media.py:73-90`): log `response.status_code`, served `content_type`, `open_upstream` elapsed before returning `StreamingResponse`.
- **`cache-eviction race` level bump** (`media.py:59`): DEBUG → INFO (sustained fallthroughs are invisible at the default level).
- **`partial-cursor 422` log** (`items.py:84-88`): add `logger.debug(f"list_items: 422, partial cursor ...")` before the raise.
- **`reddit_feeds` success** (`reddit_feeds.py:44`): add `len(resp.content)` + `resp.headers.get('content-type', '?')` to the debug line.
- **`except Exception` traceback** (`media.py:82`): `logger.warning(...)` → `logger.exception(...)` (matches `reddit_feeds.py:27,39`).

**TDD:** `test_request_id.py` — a request gets an `X-Request-ID` header; two concurrent requests get different ids; a log line from a handler includes the id (use `caplog`). Existing tests stay green.

### Theme 5 — Cache-name dedup
**Files:** `src/media/cache.py`, `src/api/items.py`, `tests/test_cache.py`

- Add `cache_name(url: str) -> str` in `src/media/cache.py` implemented as `_cache_path(url).name`. Single source of the cache naming contract.
- `items.py:45` → `item["cached"] = cache_name(item["media_url"]) in cached_names`.

**TDD:** `test_cache.py` — `cache_name(url)` equals the digest items.py was computing; if `_cache_path`'s scheme changes, both flip together (the regression the review named: silent `cached=False` re-downloads).

### Theme 6 — Tests
**Files:** `tests/test_api.py`, `tests/test_fetch.py`

- **MAJOR** — `mark_seen` asserts `items.seen_at == seen_media.seen_at` (the F11 invariant). After the POST, SELECT both and assert equality on the timestamp string.
- **MAJOR** — `mark_seen` INSERT OR REPLACE failure rolls back the UPDATE. Monkeypatch the second `db.execute` (or the `seen_media` INSERT) to raise; assert `items.seen_at` is still NULL and the response is an error (500), not a 200 with half-committed state.
- **MAJOR** — cursor-rank derivation for a pruned anchor. Insert N items, fetch page 1, DELETE the page-1 last item (the anchor) before requesting page 2 with that anchor's values; assert page 2 returns exactly the post-anchor items with no duplicates of page 1.
- **MINOR** — the two 502 paths are distinguishable: existing 502 tests assert `resp.json()["detail"]`; add a test whose transport raises `httpx.ConnectError` (respx `side_effect`) covering the generic `except Exception` path.
- **MINOR** — `prefetch_hint` awaits background tasks and asserts the next-ahead item's URL is cached (`cache_read(url) is not None`).
- **NIT** — zero-item feed asserts `item_count == 0` and `unseen_count == 0` (LEFT JOIN + `COUNT(CASE WHEN ...)` edge).
- **NIT** — size boundary 1 and 200 return 200 (parametrize the existing `test_items_rejects_invalid_size`).

## Merge order

1 → 2 → (3, 4, 5 in any order) → 6. Themes 1 + 2 close the BLOCKER. Theme 3 before 6 helps the prefetch BaseModel test.

## Out of scope

- The README/CLAUDE.md "no auth" non-goal doc update (the auth stack ships and is on by default). Worth a follow-up doc PR but not required to close the review findings.
