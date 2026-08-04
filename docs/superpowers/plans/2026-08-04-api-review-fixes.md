# src/api review fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding from the 2026-08-04 deep review of `src/api/` (1 blocker, 7 major, 11 minor, 2 nit) by adding a url-lookup gate + DNS IP pinning to the media proxy, failing fast on empty auth defaults, tightening input validation and typing, filling observability gaps, deduplicating the cache-name scheme, and closing the test gaps.

**Architecture:** Six end-to-end themes, each a self-contained PR. Themes 1 + 2 together close the BLOCKER (open proxy gated by forgeable default auth). Themes 3–6 (typing, observability, cache-name dedup, tests) follow in any order. No new dependencies; FastAPI's pydantic v2, httpx, aiosqlite, itsdangerous are already declared. The only net-new runtime component is a `request_id` middleware (theme 4).

**Tech Stack:** Python 3.14 (`requires-python = ">=3.14"`), FastAPI + pydantic v2, aiosqlite, httpx, itsdangerous. Ruff (`select = E,W,F,I,UP,B,SIM,ANN,ASYNC`), pytest + pytest-asyncio (asyncio_mode=auto) + respx. Tests run via `uv run pytest`; lint via `uv run ruff check .`; format via `uv run ruff format .`.

---

## File Structure

**New files:**
- `tests/test_config.py` — config validation tests (theme 1).
- `src/api/schemas.py` — `PrefetchHint` (BaseModel) + `ItemOut`/`FeedOut`/`SeenResponse`/`PrefetchHintResponse` (TypedDicts) (theme 3).
- `src/request_id.py` — contextvar + middleware + accessor (theme 4).
- `tests/test_request_id.py` — correlation-id tests (theme 4).
- `tests/test_cache.py` — `cache_name` dedup tests (theme 5). (If `tests/test_cache.py` already exists, append to it.)

**Modified files:**
- `src/config.py` — empty-credential fail-fast (theme 1).
- `src/media/availability.py` — `is_known_media_url` (theme 2).
- `src/api/media.py` — url-lookup gate, `db` dep, `PrefetchHint` body, TypedDict returns, `path.stat()`, observability (themes 2, 3, 4).
- `src/media/fetch.py` — DNS IP pinning in `_check_url`/`open_upstream`; `tee_to_cache` mid-stream log + mislabel fix; `request_id` threading (themes 2, 4).
- `src/api/reddit_feeds.py` — `follow_redirects=False`, nosniff, success content-type log (themes 2, 4).
- `src/db/connection.py` — hoist `_DbDep` (theme 3).
- `src/api/items.py`, `src/api/feeds.py`, `src/auth/routes.py` — import hoisted `_DbDep`, drop `= None`/`# type: ignore` cargo, TypedDict returns, drop `rn`, observability (themes 3, 4).
- `src/media/cache.py` — `cache_name` helper (theme 5).
- `src/main.py` — wire `RequestIDMiddleware` (theme 4).
- `tests/conftest.py` — add `RequestIDMiddleware` to test apps (theme 4).
- `tests/test_api.py`, `tests/test_fetch.py` — test gaps + updates for the url-lookup gate (themes 2, 6).
- `pyproject.toml` — add `"S"` to ruff `select` (theme 2).

---

## Theme 1 — Auth hardening

### Task 1: Fail-fast on empty auth credentials at startup

**Files:**
- Modify: `src/config.py:78-88`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import os
from collections.abc import Iterator

import pytest

from src.config import Settings, _load_settings


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.startswith("AUTH_") or key in ("DB_PATH", "CACHE_DIR", "OPML_PATH", "FEEDS_DIR"):
            monkeypatch.delenv(key, raising=False)
    yield None


def test_empty_auth_secret_key_raises(_clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY"):
        _load_settings()


def test_set_auth_secret_key_loads(_clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.setenv("AUTH_PASSWORD", "p")
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    s = _load_settings()
    assert s.auth_secret_key == "x" * 32


def test_one_empty_credential_raises(_clean_env: None) -> None:
    monkeypatch.setenv("AUTH_USERNAME", "u")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    with pytest.raises(RuntimeError, match="AUTH_USERNAME and AUTH_PASSWORD"):
        _load_settings()


def test_both_credentials_empty_with_key_loads(_clean_env: None) -> None:
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("AUTH_SECRET_KEY", "x" * 32)
    s = _load_settings()
    assert s.auth_username == "" and s.auth_password == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 FAIL with `RuntimeError` not raised (the function currently returns `Settings` with empty defaults).

- [ ] **Step 3: Implement the validation**

Modify `src/config.py:78-88` — replace the `_load_settings` body's return with construction + validation:

```python
def _load_settings() -> Settings:
    kwargs: dict[str, str | int] = {}
    for f in fields(Settings):
        env_val = os.environ.get(f.name.upper())
        if env_val is None:
            continue
        if f.type is int:
            kwargs[f.name] = int(env_val)
        else:
            kwargs[f.name] = env_val
    s = Settings(**kwargs)
    # Fail fast at startup: an empty session signer is forgeable, and a single
    # empty credential silently turns compare_digest("", "") into a free login.
    if not s.auth_secret_key:
        raise RuntimeError("AUTH_SECRET_KEY must be set; the session signer must not be empty")
    if (bool(s.auth_username)) != (bool(s.auth_password)):
        raise RuntimeError(
            "AUTH_USERNAME and AUTH_PASSWORD must both be set, or both empty (no-auth mode)"
        )
    return s
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Verify the whole suite still loads**

Run: `uv run pytest -q`
Expected: no new failures (the suite's `conftest.py:4-6` already sets `AUTH_SECRET_KEY`/`AUTH_USERNAME`/`AUTH_PASSWORD`, so module import stays valid).

- [ ] **Step 6: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "fix(config): fail fast on empty auth secret key or mismatched credentials"
```

---

## Theme 2 — Proxy SSRF + DNS pinning

### Task 2: Add `is_known_media_url` to availability

**Files:**
- Modify: `src/media/availability.py` (append after `_item_urls`, ~line 63)
- Test: `tests/test_api.py` (a new test added in Task 3 uses this; here we add a direct unit test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:

```python
async def test_is_known_media_url_primary_and_gallery(db: aiosqlite.Connection) -> None:
    from src.media.availability import is_known_media_url

    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f1', 'http://x', 'X')")
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type, media_json)"
        " VALUES ('i1', 'f1', 'g1', 'http://primary.jpg', 'image',"
        " '[{\"url\":\"http://slide-a.jpg\",\"type\":\"image\"},{\"url\":\"http://slide-b.jpg\",\"type\":\"image\"}]')"
    )
    await db.commit()
    assert await is_known_media_url("http://primary.jpg", db) is True
    assert await is_known_media_url("http://slide-b.jpg", db) is True
    assert await is_known_media_url("http://not-in-items.jpg", db) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_api.py::test_is_known_media_url_primary_and_gallery -v`
Expected: FAIL with `ImportError: cannot import name 'is_known_media_url'`.

- [ ] **Step 3: Implement `is_known_media_url`**

Add to `src/media/availability.py` after `_item_urls` (after line 62):

```python
def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def is_known_media_url(url: str, db: aiosqlite.Connection) -> bool:
    """True if `url` is the primary media_url of some item, or any slide of a gallery.

    Two-tier: the indexed primary lookup covers single-media items and a
    gallery's primary URL; the media_json scan covers gallery slide URLs that
    live only in the JSON array. Exact membership is verified in Python after
    the LIKE prefilter, so LIKE special characters in `url` cannot cause a
    false negative to slip past (the LIKE is a prefilter only).
    """
    async with db.execute("SELECT 1 FROM items WHERE media_url = ? LIMIT 1", (url,)) as cur:
        if await cur.fetchone() is not None:
            return True
    pattern = f'%"{_escape_like(url)}"%'
    async with db.execute(
        f"SELECT media_json FROM items WHERE media_json LIKE ? ESCAPE '\\'", (pattern,)
    ) as cur:
        for row in await cur.fetchall():
            if url in _item_urls(row):
                return True
    return False
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_api.py::test_is_known_media_url_primary_and_gallery -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/media/availability.py tests/test_api.py
git commit -m "feat(availability): add is_known_media_url two-tier url lookup"
```

### Task 3: Gate proxy_media on the url lookup (the BLOCKER's proxy side)

**Files:**
- Modify: `src/api/media.py:24-90` (add `db: _DbDep` dep + the gate)
- Test: `tests/test_api.py` (new 404 test + update the existing proxy tests to insert the url into items)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:

```python
async def test_proxy_rejects_unknown_url(
    client: AsyncClient, tmp_path: object, monkeypatch: object
) -> None:
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/not-in-db.jpg"
    resp = await client.get(f"/api/media/proxy?url={url}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not a known media url"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_api.py::test_proxy_rejects_unknown_url -v`
Expected: FAIL — currently the proxy streams from upstream (respx not installed for this url, so it 500s or hangs rather than 404ing).

- [ ] **Step 3: Add the `db` dependency and the gate to `proxy_media`**

Modify `src/api/media.py`. Change the signature at line 24-28 to add `db: _DbDep`, and insert the lookup right after the docstring (before `path = cache_read(url)` at line 50). The new top of the function body:

```python
@router.get("/media/proxy", response_model=None)
async def proxy_media(
    url: str = Query(...),
    item_id: str | None = Query(None),
    db: _DbDep = None,  # type: ignore[assignment]
) -> Response:
    """...docstring unchanged..."""
    if not await is_known_media_url(url, db):
        logger.debug(f"proxy_media: refusing unknown url {url}")
        raise HTTPException(status_code=404, detail="not a known media url")
    path = cache_read(url)
    # ...rest unchanged...
```

Add the import at the top of `src/api/media.py`:
```python
from src.media.availability import is_known_media_url
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `uv run pytest tests/test_api.py::test_proxy_rejects_unknown_url -v`
Expected: PASS.

- [ ] **Step 5: Update the existing proxy tests to insert the url into items**

The 18 existing proxy tests call `/api/media/proxy?url={url}` with urls not present in `items`. Each must now insert the url into `items.media_url` (or `media_json`) before the request, or they will 404. For each existing proxy test in `tests/test_api.py` (lines ~264, 279, 304, 328, 356, 379, 404, 430, 490, 525, 913, 936, 961, 1061, 1085), add at the top of the test (after the cache_dir monkeypatch, before the request):

```python
    await db.execute(
        "INSERT INTO feeds(id, url, title) VALUES ('fproxy', 'http://x', 'X')"
    )
    await db.execute(
        "INSERT INTO items(id, feed_id, guid, media_url, media_type) VALUES ('iproxy', 'fproxy', 'g', %r, 'image')" % url
    )
    await db.commit()
```

Most existing proxy tests do not currently take the `db` fixture — add `db: aiosqlite.Connection` to their signature. (For gallery-slide tests that use a slide url, insert the gallery into `media_json` instead and ensure `media_url` is a different primary.)

Run: `uv run pytest tests/test_api.py -k proxy -v`
Expected: all proxy tests PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS (coverage gate `--cov-fail-under=90` must still hold).

- [ ] **Step 7: Commit**

```bash
git add src/api/media.py tests/test_api.py
git commit -m "fix(api): gate /media/proxy on items url lookup (closes open proxy)"
```

### Task 4: DNS IP pinning in `open_upstream`

**Files:**
- Modify: `src/media/fetch.py:62-182` (`_check_url` returns validated IPs; `open_upstream` builds the request pinned to the IP)
- Test: `tests/test_fetch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fetch.py`:

```python
async def test_open_upstream_refuses_dns_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose first resolution is public and second is private must be
    refused: the SSRF guard must not be TOCTOU-vulnerable to DNS rebinding."""
    import httpx
    import respx

    import src.media.fetch as fetch_mod

    calls = {"n": 0}

    def _rebinding_resolve(host: str) -> list[str]:
        calls["n"] += 1
        # _check_url resolves first (public), httpx would resolve again (private).
        return ["93.184.216.34"] if calls["n"] == 1 else ["169.254.169.254"]

    monkeypatch.setattr(fetch_mod, "_resolve", _rebinding_resolve)
    monkeypatch.setattr(fetch_mod.settings, "allow_private_media_hosts", 0)

    url = "http://rebind.example.com/x.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"}))
        async with httpx.AsyncClient() as client:
            resp = await fetch_mod.open_upstream(url, None, client)
            assert resp.status_code == 200
            await resp.aclose()
    assert calls["n"] == 1, "open_upstream must not let httpx re-resolve the host"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_fetch.py::test_open_upstream_refuses_dns_rebinding -v`
Expected: FAIL — currently `open_upstream` calls `client.send(client.build_request("GET", target, ...))` which re-resolves `target`'s host, so `calls["n"]` reaches 2 (the second resolution returns the private address, which `_check_url` would refuse — but the re-resolution happens inside httpx, *past* the guard, so httpx connects to 169.254.169.254; respx has no route for that IP → `ConnectError` → the test errors with `ConnectError` rather than `assert calls["n"] == 1`). Either way the assertion fails, confirming the TOCTOU.

- [ ] **Step 3: Make `_check_url` return the validated IPs**

Modify `src/media/fetch.py:75-109`. Change the signature to return `list[str]` and return the validated address list instead of `None`:

```python
async def _check_url(url: str) -> list[str]:
    """Return the validated public IP(s) for `url`, or raise UpstreamError.

    The caller pins the httpx request to one of these IPs (with the original
    Host header + SNI) so httpx cannot re-resolve the host and reach a
    different address than the one validated here (DNS-rebinding TOCTOU).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpstreamError(f"refusing non-http(s) URL {url}")
    host = parts.hostname
    if not host:
        raise UpstreamError(f"refusing URL with no host: {url}")
    if settings.allow_private_media_hosts:
        return await asyncio.to_thread(_resolve, host)
    try:
        addrs = await asyncio.to_thread(_resolve, host)
    except OSError as exc:
        raise UpstreamError(f"cannot resolve {host} for {url}: {exc}") from exc
    validated: list[str] = []
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            logger.warning(f"_check_url: refusing {url} — {host} resolves to non-public address {ip}")
            raise UpstreamError(f"refusing non-public address {ip} for {url}")
        validated.append(str(ip))
    return validated
```

- [ ] **Step 4: Pin the request to the validated IP in `open_upstream`**

Add a helper near the top of `fetch.py` (after the imports, before `_check_url`):

```python
def _pinned_url(original: str, ip: str) -> str:
    """Return `original` with its host replaced by the literal IP `ip`."""
    parts = urlsplit(original)
    host = parts.hostname or ""
    # Bracket IPv6 literals in the netloc; IPv4 stays bare.
    netloc = f"[{ip}]:{parts.port}" if ":" in ip and parts.port else (f"[{ip}]" if ":" in ip else (f"{ip}:{parts.port}" if parts.port else ip))
    return parts._replace(netloc=netloc, hostname=ip).geturl()
```

Modify the request loop in `open_upstream` (lines 128-144). Replace the `await _check_url(target)` + `client.send(client.build_request(...))` block with:

```python
    logger.debug(f"open_upstream: GET {url} (item_id={item_id}, timeout={UPSTREAM_TIMEOUT_S}s)")
    target = url
    for _ in range(MAX_REDIRECTS + 1):
        validated = await _check_url(target)
        pinned = _pinned_url(target, validated[0])
        host = urlsplit(target).hostname or ""
        request = client.build_request(
            "GET",
            pinned,
            timeout=UPSTREAM_TIMEOUT_S,
            headers={"Host": host} if host else None,
            extensions={"sni_hostname": host} if host else None,
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        if not response.has_redirect_location:
            break
        location = response.headers["location"]
        await response.aclose()
        target = str(response.url.join(location))
        logger.debug(f"open_upstream: {url} redirected to {target}")
    else:
        raise UpstreamError(f"more than {MAX_REDIRECTS} redirects for {url}")
```

(`httpx` honours `extensions={"sni_hostname": host}` for TLS verification against the original hostname while connecting to the literal IP in the URL.)

- [ ] **Step 5: Run the new test to verify it passes**

Run: `uv run pytest tests/test_fetch.py::test_open_upstream_refuses_dns_rebinding -v`
Expected: PASS. With IP pinning, only the first (public) resolution happens, httpx connects to the public IP, `respx` (matching by URL — see Step 6 if it does not match the pinned URL) returns 200, `resp.status_code == 200`, and `calls["n"] == 1`.

- [ ] **Step 6: Run the full fetch + api suites; adjust respx routes if pinning changes URL matching**

Run: `uv run pytest tests/test_fetch.py tests/test_api.py -q`
Expected: PASS. The autouse `_stub_dns` in conftest still returns `["93.184.216.34"]`, so pinned requests go to that IP. `respx` matches by URL string — the pinned URL replaces the host with the literal IP, so respx routes registered on the original URL may miss. If any existing `test_api.py` proxy test fails because the respx route no longer matches the pinned URL, register the route on the pinned URL (`respx.get(_pinned_url(original_url))`) or use `respx.get(url, headers__Host="host")` style matching. Only adjust routes that actually miss; most respx mocks intercept at the transport before any real connection, so pinning is usually transparent to them.

- [ ] **Step 7: Commit**

```bash
git add src/media/fetch.py tests/test_fetch.py
git commit -m "fix(fetch): pin httpx to the validated IP to close DNS-rebinding SSRF"
```

### Task 5: Fix `tee_to_cache` mid-stream abort mislabel

**Files:**
- Modify: `src/media/fetch.py:185-230`
- Test: `tests/test_fetch.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetch.py`:

```python
async def test_tee_to_cache_server_abort_logs_warning(caplog, monkeypatch) -> None:
    import httpx
    import respx
    from src.media import fetch as fetch_mod
    from src.media.cache import cache_stream_tee  # noqa: F401

    monkeypatch.setattr(fetch_mod.settings, "media_max_bytes", 4)
    url = "http://example.com/big.jpg"
    body = b"0123456789"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"}))
        async with httpx.AsyncClient() as client:
            resp = await fetch_mod.open_upstream(url, None, client)
            caplog.set_level(logging.DEBUG)
            with pytest.raises(fetch_mod.UpstreamError, match="MEDIA_MAX_BYTES"):
                async for _ in fetch_mod.tee_to_cache(url, resp):
                    pass
    assert any(r.levelno == logging.WARNING and "server aborted" in r.message for r in caplog.records), \
        "a size-check abort must log at WARNING, not be mislabelled a client disconnect"


async def test_tee_to_cache_client_disconnect_logs_debug(caplog, monkeypatch) -> None:
    import httpx
    import respx
    from src.media import fetch as fetch_mod

    monkeypatch.setattr(fetch_mod.settings, "media_max_bytes", 0)
    url = "http://example.com/stream.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"abcdefghij", headers={"content-type": "image/jpeg"}))
        async with httpx.AsyncClient() as client:
            resp = await fetch_mod.open_upstream(url, None, client)
            caplog.set_level(logging.DEBUG)
            gen = fetch_mod.tee_to_cache(url, resp)
            await gen.__anext__()  # pull one chunk then abandon
            await gen.aclose()
    assert any(r.levelno == logging.DEBUG and "client stopped reading" in r.message for r in caplog.records)
```

Add `import logging` at the top of `tests/test_fetch.py` if not present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fetch.py::test_tee_to_cache_server_abort_logs_warning tests/test_fetch.py::test_tee_to_cache_client_disconnect_logs_debug -v`
Expected: FAIL — the current `finally` logs "client stopped reading" for both cases at DEBUG.

- [ ] **Step 3: Fix the `finally` block and wrap the loop**

Replace `src/media/fetch.py:207-230` (the `try:`/`finally:` inside `with download_claim(url):`):

```python
        cached = cache_stream_tee(url, response.aiter_bytes(CHUNK_SIZE), content_type)
        server_abort = False
        try:
            async with aclosing(cached):
                try:
                    async for chunk in cached:
                        digest.update(chunk)
                        sent += len(chunk)
                        if settings.media_max_bytes and sent > settings.media_max_bytes:
                            server_abort = True
                            logger.warning(
                                f"tee_to_cache: server aborted {url} after {sent} bytes "
                                f"(over MEDIA_MAX_BYTES={settings.media_max_bytes}); client sees a truncated file"
                            )
                            raise UpstreamError(
                                f"upstream body for {url} passed MEDIA_MAX_BYTES "
                                f"({settings.media_max_bytes}) after {sent} bytes; aborting"
                            )
                        yield chunk
                except UpstreamError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"tee_to_cache: aborted {url} after {sent} bytes: {type(exc).__name__}: {exc}"
                    )
                    raise
                complete = True
        finally:
            await response.aclose()
            if complete:
                logger.debug(f"tee_to_cache: streamed {sent} bytes of {url} to client and cache")
            elif server_abort:
                pass  # already logged at WARNING above
            else:
                logger.debug(
                    f"tee_to_cache: client stopped reading {url} after {sent} bytes; "
                    "nothing cached, the prefetcher will warm it later"
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fetch.py::test_tee_to_cache_server_abort_logs_warning tests/test_fetch.py::test_tee_to_cache_client_disconnect_logs_debug -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/media/fetch.py tests/test_fetch.py
git commit -m "fix(fetch): log server-side tee abort at WARNING, stop mislabelling it a client disconnect"
```

### Task 6: Harden reddit_feeds (follow_redirects=False, nosniff)

**Files:**
- Modify: `src/api/reddit_feeds.py:22,49`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
async def test_reddit_feeds_status_has_nosniff(client: AsyncClient, monkeypatch: object) -> None:
    import httpx
    import respx

    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    with respx.mock:
        respx.get("http://rf.local/status").mock(return_value=httpx.Response(200, content=b"[]", headers={"content-type": "application/json"}))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
        resp = await client.get("/api/reddit-feeds/status")
        await real_client.aclose()
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_reddit_feeds_status_redirect_is_502(client: AsyncClient, monkeypatch: object) -> None:
    import httpx
    import respx

    from src.config import settings

    monkeypatch.setattr(settings, "reddit_feeds_api_url", "http://rf.local")
    with respx.mock:
        respx.get("http://rf.local/status").mock(return_value=httpx.Response(301, headers={"location": "http://elsewhere/status"}))
        real_client = httpx.AsyncClient()
        monkeypatch.setattr("src.api.reddit_feeds.get_http_client", lambda: real_client)
        resp = await client.get("/api/reddit-feeds/status")
        await real_client.aclose()
    assert resp.status_code == 502
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py::test_reddit_feeds_status_has_nosniff tests/test_api.py::test_reddit_feeds_status_redirect_is_502 -v`
Expected: FAIL — no nosniff header; the 301 currently raises 502 already (since `is_success` is False), but `follow_redirects=True` would follow it. Verify the redirect test fails because httpx follows to `http://elsewhere/status` (respx has no route → `ConnectError` → 502). The assertion still passes for the wrong reason — keep the test (it guards against re-enabling `follow_redirects=True`).

- [ ] **Step 3: Apply the two changes**

In `src/api/reddit_feeds.py:22`, change:
```python
        resp = await client.get(url, timeout=10, follow_redirects=True)
```
to:
```python
        resp = await client.get(url, timeout=10, follow_redirects=False)
```

In `src/api/reddit_feeds.py:49`, change:
```python
    return Response(content=resp.content, media_type="application/json")
```
to:
```python
    return Response(
        content=resp.content,
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py -k reddit_feeds -v`
Expected: PASS (including the existing success/timeout/non-json tests — the existing redirect-followed test at `test_api.py:623` needs updating: it previously asserted the followed-to URL is served; now a 301 must raise 502. Update that test to assert 502, or change the upstream mock to return 200 at the final URL with `follow_redirects=False` semantics. Inspect `tests/test_api.py:623` and adjust to the new contract.)

- [ ] **Step 5: Commit**

```bash
git add src/api/reddit_feeds.py tests/test_api.py
git commit -m "fix(api): reddit-feeds — stop following redirects, add nosniff"
```

### Task 7: Enable ruff `S` category

**Files:**
- Modify: `pyproject.toml:33`
- Test: `uv run ruff check .`

- [ ] **Step 1: Add `S` to the ruff select**

In `pyproject.toml`, change:
```toml
select = ["E", "W", "F", "I", "UP", "B", "SIM", "ANN", "ASYNC"]
```
to:
```toml
select = ["E", "W", "F", "I", "UP", "B", "SIM", "ANN", "ASYNC", "S"]
```

- [ ] **Step 2: Run ruff and review the findings**

Run: `uv run ruff check .`
Expected: several `S` findings, mostly `S105`/`S106` (hardcoded passwords in tests/conftest and test fixtures) and possibly `S113` (requests without timeout). For each finding, decide:
- If it is a test fixture password (`tests/conftest.py:5-6`, `tests/test_api.py` test strings) → append `# noqa: S106` to the line.
- If it is `S105` on a settings default that is now fail-fast-validated → the empty `""` default at `src/config.py:68-70` is the documented no-auth mode; append `# noqa: S105` with a comment.
- If `S113` flags an httpx call missing a timeout → add an explicit `timeout=` (do not noqa; real fix).

- [ ] **Step 3: Re-run until clean**

Run: `uv run ruff check .`
Expected: "All checks passed!" (or only legitimate `# noqa`-suppressed findings).

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/ tests/
git commit -m "chore(ruff): enable bandit S rules; noqa legitimate test fixtures"
```

---

## Theme 3 — Input validation & typing

### Task 8: Add `src/api/schemas.py` with `PrefetchHint` + TypedDicts

**Files:**
- Create: `src/api/schemas.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:

```python
@pytest.mark.parametrize(
    "body",
    [
        {"item_id": "x", "unseen": "false"},
        {"item_id": "x", "unseen": None},
        {"item_id": 123},
        {"unseen": True},
    ],
)
async def test_prefetch_hint_rejects_bad_body(client: AsyncClient, body: dict, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "x", "feed1")
    resp = await client.post("/api/prefetch/hint", json=body)
    assert resp.status_code == 422
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_api.py::test_prefetch_hint_rejects_bad_body -v`
Expected: FAIL — `{"item_id": "x", "unseen": "false"}` is currently accepted (bool("false") = True).

- [ ] **Step 3: Create the schemas module**

Create `src/api/schemas.py`:

```python
"""API request/response shapes.

TypedDicts name the return contracts (no response_model= — FastAPI does not
validate the output, so a row with an unexpected column does not 500; the
frontend keeps every field it reads today). PrefetchHint is a pydantic
BaseModel because it crosses the trust boundary and must be validated on
input.
"""

from typing import TypedDict

from pydantic import BaseModel


class MediaSlide(TypedDict):
    url: str
    type: str


class ItemOut(TypedDict):
    id: str
    feed_id: str
    title: str | None
    media_url: str
    media_type: str
    media: list[MediaSlide]
    pub_date: str | None
    fetched_at: str | None
    seen_at: str | None
    cached: bool


class FeedOut(TypedDict):
    id: str
    title: str
    url: str
    last_fetched_at: str | None
    item_count: int
    unseen_count: int


class SeenResponse(TypedDict):
    seen_at: str


class PrefetchHintResponse(TypedDict):
    status: str


class PrefetchHint(BaseModel):
    item_id: str
    unseen: bool = True
```

- [ ] **Step 4: Use the types in the routes**

In `src/api/media.py`, import and change `prefetch_hint`:

```python
from src.api.schemas import PrefetchHint, PrefetchHintResponse

@router.post("/prefetch/hint")
async def prefetch_hint(
    body: PrefetchHint,
    db: _DbDep = None,  # type: ignore[assignment]
) -> PrefetchHintResponse:
    item_id = body.item_id
    unseen = body.unseen
    logger.debug(f"prefetch_hint item_id={item_id} unseen={unseen}")
    if not item_id:
        logger.debug("prefetch_hint: 422, no item_id in body")
        raise HTTPException(status_code=422, detail="item_id required")
    ...
    return {"status": "ok"}
```

(Note: with the BaseModel, FastAPI validates before the handler runs, so the `if not item_id` branch becomes unreachable for empty/missing item_id — keep it as defence-in-depth, but the 422 now comes from pydantic.)

In `src/api/items.py`, import `ItemOut`, `SeenResponse` and annotate `list_items`/`mark_seen`:
```python
from src.api.schemas import ItemOut, SeenResponse

async def list_items(...) -> list[ItemOut]: ...
async def mark_seen(...) -> SeenResponse: ...
```
And in `src/api/feeds.py`:
```python
from src.api.schemas import FeedOut
async def list_feeds(...) -> list[FeedOut]: ...
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_api.py::test_prefetch_hint_rejects_bad_body tests/test_api.py -k prefetch -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite + lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS / clean.

- [ ] **Step 7: Commit**

```bash
git add src/api/schemas.py src/api/media.py src/api/items.py src/api/feeds.py tests/test_api.py
git commit -m "feat(api): PrefetchHint BaseModel + TypedDict return shapes"
```

### Task 9: Drop the `rn` column from the outer SELECT

**Files:**
- Modify: `src/api/items.py:106-107`
- Test: `tests/test_api.py` (add an assertion that `rn` is not in the response)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:

```python
async def test_items_response_omits_rn(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    data = (await client.get("/api/items")).json()
    assert data and "rn" not in data[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_api.py::test_items_response_omits_rn -v`
Expected: FAIL — `rn` is currently in the response.

- [ ] **Step 3: Drop `rn` from the SELECT**

In `src/api/items.py:106-107`, change:
```python
        SELECT id, feed_id, title, media_url, media_type, media_json,
               pub_date, fetched_at, seen_at, rn
        FROM ranked
```
to:
```python
        SELECT id, feed_id, title, media_url, media_type, media_json,
               pub_date, fetched_at, seen_at
        FROM ranked
```
(`rn` is still produced by the CTE and used in the WHERE clause at line 98; only the outer column list drops it.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_api.py::test_items_response_omits_rn -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/items.py tests/test_api.py
git commit -m "refactor(api): drop dead rn column from /items response"
```

### Task 10: Hoist `_DbDep` and drop the `= None` / `# type: ignore` cargo

**Files:**
- Modify: `src/db/connection.py` (add the alias), `src/api/items.py`, `src/api/media.py`, `src/api/feeds.py`, `src/auth/routes.py`
- Test: `uv run pytest -q` (no new test — mechanical refactor verified by the existing suite)

- [ ] **Step 1: Hoist `_DbDep` to `connection.py`**

In `src/db/connection.py`, add the imports and the alias after `get_db` (after line 49). Use the plain-assignment form (the form already used locally) — FastAPI reads the `Depends()` marker off `Annotated[...]` and a py314 `type` statement adds nothing here:

```python
from typing import Annotated

from fastapi import Depends

_DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]
```

`get_db` is already documented as the FastAPI dependency factory, so coupling `connection.py` to `fastapi.Depends` is the layering it already implies.

- [ ] **Step 2: Import the hoisted alias everywhere**

In `src/api/items.py` remove lines 10 (`import aiosqlite` if now unused — keep if still used), 11 (`from fastapi import APIRouter, Depends, HTTPException, Query` → drop `Depends` if now unused), 21 (`_DbDep = ...`), and add `from src.db.connection import get_db, _DbDep`. Do the same in `src/api/media.py` (drop line 21, import `_DbDep`), `src/api/feeds.py` (drop the inline `Annotated[aiosqlite.Connection, Depends(get_db)]` at line 16, import `_DbDep`), and `src/auth/routes.py` (drop line 41, import `_DbDep`).

- [ ] **Step 3: Drop `= None  # type: ignore[assignment]` on all six sites**

In `src/api/items.py:56,129`, `src/api/media.py:96` (and `prefetch_hint` from Task 8), and `src/auth/routes.py:41,92,131,157` — change every `db: _DbDep = None,  # type: ignore[assignment]` to `db: _DbDep,`.

- [ ] **Step 4: Run the suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format .`
Expected: PASS / clean. If `Depends` or `Annotated` imports become unused in any api module, ruff `I`/`F` will flag them — remove the unused imports.

- [ ] **Step 5: Commit**

```bash
git add src/db/connection.py src/api/items.py src/api/media.py src/api/feeds.py src/auth/routes.py
git commit -m "refactor: hoist _DbDep to connection.py, drop the = None / type:ignore cargo"
```

### Task 11: Replace `os.stat` with `path.stat()`, tighten `params` typing

**Files:**
- Modify: `src/api/media.py:4,57`, `src/api/items.py:8,91`
- Test: `uv run pytest -q`

- [ ] **Step 1: `path.stat()` in `media.py`**

In `src/api/media.py:4` remove `import os`. At line 57 change `stat_result = os.stat(path)` to `stat_result = path.stat()`. (The `path` is the `Path` returned by `cache_read`.)

- [ ] **Step 2: `params: list[str | int]` in `items.py`**

In `src/api/items.py:91` change `params: list[Any] = []` to `params: list[str | int] = []`. If `Any` is no longer used in the module, drop `from typing import Annotated, Any` → `from typing import Annotated` (Task 10 may have already removed `Annotated` in favour of the hoisted alias; re-check the import block and keep only what is used).

- [ ] **Step 3: Run the suite + lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format .`
Expected: PASS / clean.

- [ ] **Step 4: Commit**

```bash
git add src/api/media.py src/api/items.py
git commit -m "refactor(api): path.stat() over os.stat, list[str|int] params"
```

---

## Theme 4 — Observability

### Task 12: `src/request_id.py` middleware + wiring

**Files:**
- Create: `src/request_id.py`
- Modify: `src/main.py:69`, `tests/conftest.py:57-69,102-104`
- Test: `tests/test_request_id.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_request_id.py`:

```python
import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from src.request_id import RequestIDMiddleware, current_request_id


@pytest.mark.asyncio
async def test_request_id_header_set() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/")
    async def _root() -> dict[str, str]:
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) == 32


@pytest.mark.asyncio
async def test_concurrent_requests_get_distinct_ids() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/")
    async def _root() -> dict[str, str]:
        await asyncio.sleep(0)
        return {"rid": current_request_id() or ""}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1, r2 = await asyncio.gather(c.get("/"), c.get("/"))
    assert r1.json()["rid"] != r2.json()["rid"]


@pytest.mark.asyncio
async def test_handler_log_line_includes_request_id(caplog) -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    logger = logging.getLogger("test_rid")

    @app.get("/")
    async def _root() -> dict[str, str]:
        logger.info("handling", extra={"request_id": current_request_id()})
        return {"status": "ok"}

    caplog.set_level(logging.INFO, logger="test_rid")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    rec = next(r for r in caplog.records if r.getMessage() == "handling")
    assert getattr(rec, "request_id", None) is not None
    assert len(rec.request_id) == 32
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_request_id.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.request_id'`.

- [ ] **Step 3: Create the middleware**

Create `src/request_id.py`:

```python
"""Per-request correlation id, carried via a contextvar and an X-Request-ID header.

A DEBUG log run can be reassembled end-to-end across modules by filtering on
the request id. The middleware sets the contextvar on entry and the response
header on exit; log lines use `extra={"request_id": current_request_id()}` so
the id is attached without being interpolated into every f-string.
"""

import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

HEADER = "X-Request-ID"


def current_request_id() -> str | None:
    return _request_id.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = request.headers.get(HEADER.lower()) or uuid.uuid4().hex
        token = _request_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[HEADER] = rid
        return response
```

- [ ] **Step 4: Wire the middleware in `main.py` and the test apps**

In `src/main.py:9` add `from src.request_id import RequestIDMiddleware`. After line 69 (`app.add_middleware(AuthMiddleware)`), add:
```python
app.add_middleware(RequestIDMiddleware)
```
(Middleware runs in reverse order of addition; adding RequestID *after* Auth means RequestID wraps the handler inside Auth, so the id is set before the handler runs and the auth log lines also carry it.)

In `tests/conftest.py`, import `from src.request_id import RequestIDMiddleware` (after the existing auth import at line 73) and add `test_app.add_middleware(RequestIDMiddleware)` in both the `client` fixture (after line 58) and the `auth_client` fixture (after line 103, before `test_app.include_router`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_request_id.py -v && uv run pytest -q`
Expected: PASS / no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/request_id.py src/main.py tests/conftest.py tests/test_request_id.py
git commit -m "feat: per-request correlation id middleware"
```

### Task 13: DB-query duration logging in `src/api/`

**Files:**
- Modify: `src/api/items.py:114,144,153`, `src/api/feeds.py:23-32`, `src/api/media.py:110`
- Test: `tests/test_api.py` (add a duration-assert test using `caplog`)

- [ ] **Step 1: Write a representative failing test**

Append to `tests/test_api.py`:

```python
async def test_list_items_logs_db_duration(client: AsyncClient, db: aiosqlite.Connection, caplog) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    caplog.set_level(logging.DEBUG, logger="src.api.items")
    await client.get("/api/items")
    assert any("ms" in r.getMessage() and "list_items" in r.getMessage() for r in caplog.records), \
        "list_items exit log must include the DB query duration"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_api.py::test_list_items_logs_db_duration -v`
Expected: FAIL — the current exit log has no duration.

- [ ] **Step 3: Add `time.perf_counter()` brackets to each `db.execute` in `src/api/`**

In `src/api/items.py`, import `time` and wrap the list query (line 114):

```python
    import time as _time
    t0 = _time.perf_counter()
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    db_ms = (_time.perf_counter() - t0) * 1000
```
(Add `import time` at the top of the module instead of the local import; use `time.perf_counter()`.)

Update the exit log (line 122) to include `db_ms`:
```python
    logger.debug(f"list_items returned {len(items)} item(s), {cached_count} cached on disk; db={db_ms:.1f}ms")
```

Do the same for `mark_seen`'s two writes (lines 144, 153) — log before/after each with the target table:
```python
    t0 = time.perf_counter()
    async with db.execute("UPDATE items SET seen_at = ? WHERE id = ? RETURNING media_url, seen_at", (now, item_id)) as cur:
        row = await cur.fetchone()
    update_ms = (time.perf_counter() - t0) * 1000
    ...
    t1 = time.perf_counter()
    await db.execute("INSERT OR REPLACE INTO seen_media (media_key, seen_at) VALUES (?, ?)", (media_key(row["media_url"]), now))
    insert_ms = (time.perf_counter() - t1) * 1000
    t2 = time.perf_counter()
    await db.commit()
    commit_ms = (time.perf_counter() - t2) * 1000
    logger.debug(f"mark_seen item_id={item_id} seen_at={row['seen_at']} update={update_ms:.1f}ms insert={insert_ms:.1f}ms commit={commit_ms:.1f}ms")
```

In `src/api/feeds.py`, wrap the query (line 23-32) and include `db_ms` in the exit log:
```python
    t0 = time.perf_counter()
    async with db.execute("""SELECT ...""") as cur:
        rows = await cur.fetchall()
    db_ms = (time.perf_counter() - t0) * 1000
    logger.debug(f"list_feeds returned {len(rows)} feed(s); db={db_ms:.1f}ms")
```
(add `import time` at the top of feeds.py).

In `src/api/media.py:110`, wrap the existence check:
```python
    t0 = time.perf_counter()
    async with db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)) as cur:
        exists = await cur.fetchone() is not None
    logger.debug(f"prefetch_hint item_id={item_id} exists={exists} db={(time.perf_counter()-t0)*1000:.1f}ms")
```
(add `import time` at the top of media.py).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_api.py::test_list_items_logs_db_duration -v && uv run pytest -q`
Expected: PASS / no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/api/items.py src/api/feeds.py src/api/media.py tests/test_api.py
git commit -m "feat(api): log DB query duration at every src/api boundary"
```

### Task 14: `cache_present_names` boundary log + other `src/api` log fixes

**Files:**
- Modify: `src/api/items.py:84-88,116`, `src/api/media.py:59,73-90,82`, `src/api/reddit_feeds.py:44`
- Test: `tests/test_api.py` (one assertion per fix using `caplog`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
async def test_list_items_logs_partial_cursor_422(client: AsyncClient, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="src.api.items")
    resp = await client.get("/api/items", params={"after_feed_id": "f1"})
    assert resp.status_code == 422
    assert any("422" in r.getMessage() and "partial cursor" in r.getMessage() for r in caplog.records)


async def test_proxy_eviction_fallthrough_logs_info(client: AsyncClient, db: aiosqlite.Connection, tmp_path, monkeypatch, caplog) -> None:
    import hashlib
    import httpx
    import respx
    import src.media.cache as cache_mod

    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/evicted.jpg"
    fname = hashlib.sha256(url.encode()).hexdigest()
    (tmp_path / fname).write_bytes(b"")  # type: ignore[operator]
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',%r,'image')" % url)
    await db.commit()
    # delete the file between cache_read and os.stat by stubbing cache_read to return the path
    import src.api.media as media_mod
    orig_read = media_mod.cache_read
    def _vanish(u):
        p = orig_read(u)
        if p:
            p.unlink(missing_ok=True)
        return p
    monkeypatch.setattr(media_mod, "cache_read", _vanish)
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"}))
        real = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real)
        caplog.set_level(logging.INFO, logger="src.api.media")
        await client.get(f"/api/media/proxy?url={url}")
        await real.aclose()
    assert any(r.levelno == logging.INFO and "evicted" in r.getMessage() for r in caplog.records)


async def test_proxy_exception_uses_logger_exception(client: AsyncClient, db, tmp_path, monkeypatch, caplog) -> None:
    import httpx, respx, src.media.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/transport.jpg"
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',%r,'image')" % url)
    await db.commit()
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.ConnectError("boom"))
        real = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real)
        caplog.set_level(logging.WARNING, logger="src.api.media")
        await client.get(f"/api/media/proxy?url={url}")
        await real.aclose()
    assert any(r.levelno == logging.WARNING and r.exc_info for r in caplog.records), \
        "the generic except must use logger.exception (exc_info set)"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py::test_list_items_logs_partial_cursor_422 tests/test_api.py::test_proxy_eviction_fallthrough_logs_info tests/test_api.py::test_proxy_exception_uses_logger_exception -v`
Expected: FAIL.

- [ ] **Step 3: Apply the log fixes**

In `src/api/items.py:84-88` add a debug line before the raise:
```python
    if any(part is not None for part in cursor) and not all(part is not None for part in cursor):
        logger.debug(f"list_items: 422, partial cursor after_feed_id={after_feed_id} after_pub_date={after_pub_date} after_id={after_id}")
        raise HTTPException(...)
```

In `src/api/items.py:116` bracket `cache_present_names`:
```python
    t_cache = time.perf_counter()
    cached_names = await asyncio.to_thread(cache_present_names)
    cache_ms = (time.perf_counter() - t_cache) * 1000
    logger.debug(f"list_items: cache_present_names returned {len(cached_names)} name(s) in {cache_ms:.1f}ms")
```

In `src/api/media.py:59` change `logger.debug(...)` to `logger.info(...)`.

In `src/api/media.py:73-90`, bracket `open_upstream` with `time.perf_counter()` and add the success exit log before the `return StreamingResponse`:
```python
    t_up = time.perf_counter()
    response = await open_upstream(url, item_id, client, request_id=current_request_id())
    upstream_ms = (time.perf_counter() - t_up) * 1000
    content_type = response.headers.get("content-type", "application/octet-stream")
    logger.debug(
        f"proxy_media: MISS ok {url} -> {response.status_code} type={content_type} "
        f"upstream={upstream_ms:.1f}ms (request_id={current_request_id()})"
    )
    return StreamingResponse(...)
```
Change the `except Exception` at line 82 from `logger.warning(...)` to `logger.exception(...)` (drop the `type(exc).__name__: {exc}` suffix — `exc_info` carries the traceback).

In `src/api/reddit_feeds.py:44`, add body size + content-type:
```python
    logger.debug(
        f"reddit_feeds_status {resp.status_code} from {url} in {elapsed_ms:.0f}ms "
        f"bytes={len(resp.content)} type={resp.headers.get('content-type', '?')}"
    )
```

Thread `request_id` into `open_upstream`/`tee_to_cache` log lines: add an optional `request_id: str | None = None` param to both, pass `current_request_id()` from `proxy_media`, and include it in the existing debug f-strings. (`fetch_to_cache` passes `None`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py -k "partial_cursor or eviction or exception_uses" -v && uv run pytest -q`
Expected: PASS / no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/api/items.py src/api/media.py src/api/reddit_feeds.py src/media/fetch.py tests/test_api.py
git commit -m "feat(api): observability — partial-cursor log, cache boundary log, eviction at INFO, logger.exception, success exit log, request_id threading"
```

---

## Theme 5 — Cache-name dedup

### Task 15: Expose `cache_name` and use it in `items.py`

**Files:**
- Modify: `src/media/cache.py` (add `cache_name` after `_cache_path`), `src/api/items.py:45`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cache.py` (append if it exists):

```python
import hashlib

from src.media.cache import _cache_path, cache_name


def test_cache_name_matches_cache_path_name() -> None:
    url = "http://example.com/x.jpg"
    assert cache_name(url) == _cache_path(url).name
    assert cache_name(url) == hashlib.sha256(url.encode()).hexdigest()


def test_cache_name_is_the_single_source() -> None:
    # If _cache_path's scheme ever changes, cache_name must follow it — this
    # test fails the moment the two diverge, which is the regression the
    # review named (silent cached=False re-downloads).
    url = "http://example.com/y.png"
    assert cache_name(url) == _cache_path(url).name
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cache.py -v`
Expected: FAIL with `ImportError: cannot import name 'cache_name'`.

- [ ] **Step 3: Add `cache_name`**

In `src/media/cache.py` after `_cache_path` (line 40):

```python
def cache_name(url: str) -> str:
    """The cache filename for `url` (sha256 hex, no extension).

    Single source of the naming contract: items.py compares this against
    `cache_present_names()`'s set to set the `cached` hint, so the two must
    not drift. Implemented as `_cache_path(url).name` so a scheme change here
    flips both sides at once.
    """
    return _cache_path(url).name
```

- [ ] **Step 4: Use it in `items.py`**

In `src/api/items.py:45` change:
```python
    item["cached"] = hashlib.sha256(item["media_url"].encode()).hexdigest() in cached_names
```
to:
```python
    item["cached"] = cache_name(item["media_url"]) in cached_names
```
Add `from src.media.cache import cache_name` to the imports (drop the `import hashlib` import at items.py:5 if now unused).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cache.py tests/test_api.py -q && uv run ruff check .`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add src/media/cache.py src/api/items.py tests/test_cache.py
git commit -m "refactor(cache): expose cache_name as the single source of the naming scheme"
```

---

## Theme 6 — Tests

### Task 16: `mark_seen` F11 invariant + INSERT-rollback atomicity

**Files:**
- Modify: `tests/test_api.py`
- Test: (these are the tests)

- [ ] **Step 1: Write the F11 invariant test**

Append to `tests/test_api.py`:

```python
async def test_mark_seen_items_and_seen_media_share_timestamp(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """F11: items.seen_at and seen_media.seen_at are bound to one `now` so they
    cannot diverge. A refactor that binds a second dt.now() to the INSERT must
    fail this test."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    resp = await client.post("/api/items/item1/seen")
    assert resp.status_code == 200
    async with db.execute("SELECT seen_at FROM items WHERE id = 'item1'") as cur:
        items_seen = (await cur.fetchone())[0]
    async with db.execute("SELECT seen_at FROM seen_media WHERE media_key = 'http://example.com/img.jpg'") as cur:
        media_seen = (await cur.fetchone())[0]
    assert items_seen == media_seen, f"items.seen_at ({items_seen}) != seen_media.seen_at ({media_seen})"
```

- [ ] **Step 2: Write the rollback atomicity test**

```python
async def test_mark_seen_rolls_back_when_seen_media_write_fails(client: AsyncClient, db: aiosqlite.Connection, monkeypatch) -> None:
    """If the INSERT OR REPLACE INTO seen_media raises, the UPDATE must not
    be left committed — otherwise items.seen_at is set with no durable seen
    record."""
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1", seen_at=None)
    orig_execute = db.execute

    calls = {"n": 0}
    async def _flaky_execute(query, params=()):
        calls["n"] += 1
        # Let the UPDATE...RETURNING through; fail the seen_media INSERT.
        if "INSERT OR REPLACE INTO seen_media" in query:
            raise aiosqlite.OperationalError("simulated disk full")
        return await orig_execute(query, params)

    monkeypatch.setattr(db, "execute", _flaky_execute)
    resp = await client.post("/api/items/item1/seen")
    monkeypatch.undo()  # so the SELECT below uses the real connection
    assert resp.status_code == 500
    async with db.execute("SELECT seen_at FROM items WHERE id = 'item1'") as cur:
        row = await cur.fetchone()
    assert row[0] is None, "items.seen_at must be rolled back when seen_media write fails"
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_api.py::test_mark_seen_items_and_seen_media_share_timestamp tests/test_api.py::test_mark_seen_rolls_back_when_seen_media_write_fails -v`
Expected: the invariant test PASSES (current code already binds one `now`); the rollback test may FAIL if the request-scoped connection's autocommit leaves the UPDATE in place. If it fails, the fix is to ensure `mark_seen` does not commit between the two writes (it currently does not — it commits once at the end, so the rollback on connection close already works). Verify the test PASSES; if it does not, wrap the two writes in a single transaction by removing any intermediate `commit` (there is none today, so the test should pass as-is).

- [ ] **Step 4: Commit**

```bash
git add tests/test_api.py
git commit -m "test(api): assert mark_seen F11 invariant + INSERT-failure rollback"
```

### Task 17: Cursor-rank derivation for a pruned anchor

**Files:**
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_api.py`:

```python
async def test_items_cursor_survives_pruned_anchor(client: AsyncClient, db: aiosqlite.Connection) -> None:
    """The docstring's central edge case: the anchor row itself is pruned
    between page-1 and page-2. The COUNT(*)-derived rank must still place the
    cursor at the anchor's position, so page-2 returns exactly the post-anchor
    items with no duplicates of page-1."""
    await _insert_feed(db)
    for i in range(1, 6):
        await _insert_item(db, f"item{i}", "feed1")
    page1 = (await client.get("/api/items", params={"size": 3})).json()
    assert [i["id"] for i in page1] == ["item1", "item2", "item3"]
    anchor = page1[-1]  # item3
    # Prune the anchor itself (as prune_items would).
    await db.execute("DELETE FROM items WHERE id = 'item3'")
    await db.commit()
    page2 = (
        await client.get(
            "/api/items",
            params={
                "after_feed_id": anchor["feed_id"],
                "after_pub_date": anchor["pub_date"],
                "after_id": anchor["id"],
                "size": 10,
            },
        )
    ).json()
    assert [i["id"] for i in page2] == ["item4", "item5"], (
        f"pruned-anchor cursor must not re-emit page-1 items or skip ahead; got {[i['id'] for i in page2]}"
    )
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_api.py::test_items_cursor_survives_pruned_anchor -v`
Expected: PASS (the current count-derived rank already handles this per the docstring). If it FAILS, the docstring's claim is wrong and the implementation needs a fix — file as a follow-up bug, but for the plan's purposes the test pins the behaviour.

- [ ] **Step 3: Commit**

```bash
git add tests/test_api.py
git commit -m "test(api): cursor-rank derivation for a pruned anchor"
```

### Task 18: Distinguishable 502 paths + prefetch warm runs

**Files:**
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the 502-detail + transport-exception tests**

Append to `tests/test_api.py`:

```python
async def test_proxy_upstream_error_detail(client: AsyncClient, db, tmp_path, monkeypatch) -> None:
    import httpx, respx, src.media.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/missing.jpg"
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',%r,'image')" % url)
    await db.commit()
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        real = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real.aclose()
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream error"


async def test_proxy_transport_error_detail(client: AsyncClient, db, tmp_path, monkeypatch) -> None:
    import httpx, respx, src.media.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    url = "http://example.com/unreachable.jpg"
    await db.execute("INSERT INTO feeds(id,url,title) VALUES ('f','http://x','X')")
    await db.execute("INSERT INTO items(id,feed_id,guid,media_url,media_type) VALUES ('i','f','g',%r,'image')" % url)
    await db.commit()
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.ConnectError("boom"))
        real = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real)
        resp = await client.get(f"/api/media/proxy?url={url}")
        await real.aclose()
    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream fetch failed"


async def test_prefetch_hint_warms_cache(client: AsyncClient, db, tmp_path, monkeypatch) -> None:
    import asyncio, httpx, respx, src.media.cache as cache_mod, src.media.media as _  # noqa: F401
    monkeypatch.setattr(cache_mod.settings, "cache_dir", str(tmp_path))
    await _insert_feed(db)
    # two items so prefetch_ahead has something to warm
    await _insert_item(db, "item1", "feed1")
    await _insert_item(db, "item2", "feed1")
    url = "http://example.com/img.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"}))
        real = httpx.AsyncClient()
        monkeypatch.setattr("src.api.media.get_http_client", lambda: real)
        resp = await client.post("/api/prefetch/hint", json={"item_id": "item1", "unseen": True})
        assert resp.status_code == 200
        # let the background warm task finish
        from src.media import prefetch as _pf
        if _pf._bg_tasks:
            await asyncio.gather(*_pf._bg_tasks, return_exceptions=True)
        await real.aclose()
    from src.media.cache import cache_read
    assert cache_read(url) is not None, "prefetch_hint must actually warm the cache"
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_api.py::test_proxy_upstream_error_detail tests/test_api.py::test_proxy_transport_error_detail tests/test_api.py::test_prefetch_hint_warms_cache -v`
Expected: the 502-detail tests PASS (current code uses those detail strings). The prefetch-warm test may FAIL if the background task is not awaited the same way; adjust `_bg_tasks` access to match `src/media/prefetch.py`'s actual task-tracking attribute (inspect `src/media/prefetch.py` and use the real attribute name).

- [ ] **Step 3: Commit**

```bash
git add tests/test_api.py
git commit -m "test(api): distinguishable 502 details + prefetch actually warms cache"
```

### Task 19: Zero-item feed counts + size boundaries (nits)

**Files:**
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the tests**

Append to `tests/test_api.py`:

```python
async def test_feeds_zero_item_counts(client: AsyncClient, db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO feeds(id, url, title) VALUES ('f0', 'http://x', 'Empty')")
    await db.commit()
    data = (await client.get("/api/feeds")).json()
    feed = next(f for f in data if f["id"] == "f0")
    assert feed["item_count"] == 0
    assert feed["unseen_count"] == 0


@pytest.mark.parametrize("size", [1, 200])
async def test_items_accepts_size_boundaries(client: AsyncClient, db: aiosqlite.Connection, size: int) -> None:
    await _insert_feed(db)
    await _insert_item(db, "item1", "feed1")
    resp = await client.get("/api/items", params={"size": size})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_api.py::test_feeds_zero_item_counts tests/test_api.py::test_items_accepts_size_boundaries -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_api.py
git commit -m "test(api): zero-item feed counts + size boundary 1/200"
```

---

## Final verification

- [ ] **Step 1: Full suite + lint + format**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS / clean. Coverage gate `--cov-fail-under=90` must hold.

- [ ] **Step 2: Manual smoke**

Run: `uv run uvicorn src.main:app --port 8080` — confirm startup refuses without `AUTH_SECRET_KEY` set, then restart with it set and hit `/api/media/proxy?url=<unknown>` → 404.

- [ ] **Step 3: Re-verify the BLOCKER is closed**

Confirm: (a) `/api/media/proxy` 404s an unknown url; (b) startup raises without `AUTH_SECRET_KEY`; (c) `/login` with empty creds against a set key returns 401.

---

## Merge order

Theme 1 → Theme 2 → (3, 4, 5 in any order) → 6. Themes 1 + 2 close the BLOCKER. Theme 3 before 6 helps the `PrefetchHint` test.
