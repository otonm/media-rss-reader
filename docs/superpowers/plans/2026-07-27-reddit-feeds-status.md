# Reddit Feeds Status Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a modal overlay accessible from the controls menu that displays operational status of the Reddit Feeds companion service by proxying its `GET /status` endpoint through the backend.

**Architecture:** A new backend route `GET /api/reddit-feeds/status` proxies the external Reddit Feeds API via `httpx.AsyncClient` (already shared via `scheduler.get_http_client()`). The frontend adds a 📊 button to the controls bar; clicking it opens a modal that fetches and renders a table of feed statuses.

**Tech Stack:** FastAPI, httpx, vanilla JS/CSS (no libraries added)

---

### Task 1: Add `reddit_feeds_api_url` config

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: Add the field to Settings**

Add after the `auth_lockout_minutes` field (line 45):

```python
    # --- Reddit Feeds integration ---
    reddit_feeds_api_url: str = "http://127.0.0.1:9090"
```

- [ ] **Step 2: Commit**

```bash
git add src/config.py
git commit -m "feat: add REDDIT_FEEDS_API_URL config"
```

---

### Task 2: Create backend proxy route

**Files:**
- Create: `src/api/reddit_feeds.py`

- [ ] **Step 1: Write the file**

```python
"""GET /api/reddit-feeds/status — proxy the Reddit Feeds status endpoint."""

from fastapi import APIRouter, HTTPException

from src.config import settings
from src.scheduler import get_http_client

router = APIRouter()


@router.get("/reddit-feeds/status")
async def reddit_feeds_status() -> dict:
    client = get_http_client()
    url = f"{settings.reddit_feeds_api_url.rstrip('/')}/status"
    try:
        resp = await client.get(url)
    except Exception:
        raise HTTPException(status_code=502, detail="Reddit Feeds API unreachable")
    if resp.is_error:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()
```

- [ ] **Step 2: Commit**

```bash
git add src/api/reddit_feeds.py
git commit -m "feat: add Reddit Feeds status proxy route"
```

---

### Task 3: Register the router in main.py

**Files:**
- Modify: `src/main.py:12`

- [ ] **Step 1: Import and register the router**

Add the import after line 12 (`from src.api import feeds, items, media`):

```python
from src.api import feeds, items, media, reddit_feeds
```

Add the router registration after line 60 (`app.include_router(media.router, prefix="/api")`):

```python
app.include_router(reddit_feeds.router, prefix="/api")
```

- [ ] **Step 2: Commit**

```bash
git add src/main.py
git commit -m "feat: register Reddit Feeds status router"
```

---

### Task 4: Write backend tests

**Files:**
- Modify: `tests/test_api.py` (new tests)
- Modify: `tests/conftest.py` (register the new router in `client` fixture)

- [ ] **Step 1: Register the router in conftest.py**

In `tests/conftest.py`, add the import (after existing imports on line 18):

```python
from src.api import reddit_feeds as reddit_feeds_router
```

In the `client` fixture (after line 45), add:

```python
    test_app.include_router(reddit_feeds_router.router, prefix="/api")
```

- [ ] **Step 2: Add tests for the status proxy endpoint**

Add these tests to `tests/test_api.py` (before the interleaved test at line 334):

```python
# ---------------------------------------------------------------------------
# GET /api/reddit-feeds/status tests
# ---------------------------------------------------------------------------


async def test_reddit_feeds_status_success(client: AsyncClient, mock_http: respx.MockRouter) -> None:
    upstream_json = {
        "feeds": [
            {
                "name": "EarthPorn",
                "last_status": "success",
                "last_fetch": "2026-07-27T14:02:00.123456+00:00",
                "last_item_count": 5,
                "total_items": 42,
            }
        ],
        "last_run": "2026-07-27T14:02:05.654321+00:00",
    }
    mock_http.get("http://127.0.0.1:9090/status").mock(
        return_value=httpx.Response(200, json=upstream_json)
    )
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 200
    assert resp.json() == upstream_json


async def test_reddit_feeds_status_unreachable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx as httpx_mod
    import src.api.reddit_feeds as rf_mod

    fake_client = httpx_mod.AsyncClient()

    async def fake_get(url: str) -> httpx_mod.Response:
        raise httpx_mod.ConnectError("Connection refused")

    fake_client.get = fake_get  # type: ignore[method-assign]
    monkeypatch.setattr(rf_mod, "get_http_client", lambda: fake_client)

    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 502


async def test_reddit_feeds_status_upstream_error(client: AsyncClient, mock_http: respx.MockRouter) -> None:
    mock_http.get("http://127.0.0.1:9090/status").mock(
        return_value=httpx.Response(500, json={"error": "Failed to read status data"})
    )
    resp = await client.get("/api/reddit-feeds/status")
    assert resp.status_code == 500
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
uv run pytest tests/test_api.py -k reddit_feeds -v
```

Expected: 3 tests pass.

- [ ] **Step 4: Run full test suite**

```bash
uv run pytest
```

Expected: all tests pass, coverage >= 90%.

- [ ] **Step 5: Commit**

```bash
git add tests/test_api.py tests/conftest.py
git commit -m "test: add Reddit Feeds status proxy tests"
```

---

### Task 5: Add status button and modal div to index.html

**Files:**
- Modify: `src/static/index.html`

- [ ] **Step 1: Add the status button to controls**

After the `btn-show-seen` button (line 17), add:

```html
    <button id="btn-status" class="option" type="button" aria-label="Reddit Feeds status" title="Status">📊</button>
```

- [ ] **Step 2: Add the modal markup after the feed div**

After `<div id="feed" ...></div>` (line 12), add:

```html
  <div id="status-modal" class="modal-overlay" aria-hidden="true">
    <div class="modal-card">
      <button id="status-modal-close" class="modal-close" type="button" aria-label="Close">×</button>
      <div id="status-modal-body" class="modal-body"></div>
    </div>
  </div>
```

- [ ] **Step 3: Commit**

```bash
git add src/static/index.html
git commit -m "feat: add status button and modal to UI"
```

---

### Task 6: Add modal and status table CSS

**Files:**
- Modify: `src/static/style.css`

- [ ] **Step 1: Add modal overlay and card styles**

Add at the end of `style.css`:

```css
/* -----------------------------------------------------------------------
   Status modal
   ----------------------------------------------------------------------- */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(0, 0, 0, 0.7);
  align-items: center;
  justify-content: center;
}

.modal-overlay.open {
  display: flex;
}

.modal-card {
  background: var(--bg);
  border: 1px solid var(--control-border);
  border-radius: 12px;
  max-width: 600px;
  width: 90vw;
  max-height: 80vh;
  overflow-y: auto;
  position: relative;
  padding: 1.5rem;
}

.modal-close {
  position: absolute;
  top: 0.5rem;
  right: 0.75rem;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.08);
  color: var(--control-icon);
  font-size: 1.25rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}

.modal-close:hover {
  background: rgba(255, 255, 255, 0.15);
}

.modal-body {
  color: var(--fg);
}

/* --- Status table --- */
.status-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}

.status-table th {
  text-align: left;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--control-border);
  color: var(--fg-dim);
  font-weight: 500;
}

.status-table td {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  vertical-align: top;
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 0.4rem;
}

.status-dot.success { background: #4caf50; }
.status-dot.error   { background: #f44336; }

.status-footer {
  margin-top: 1rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--control-border);
  font-size: 0.8rem;
  color: var(--fg-dim);
}

.status-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem;
}

.status-loading .spinner {
  width: var(--spinner-size);
  height: var(--spinner-size);
  border: var(--spinner-border) solid var(--spinner-track);
  border-top-color: var(--spinner-head);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.status-error {
  color: #f44336;
  text-align: center;
  padding: 2rem;
}

.status-empty {
  color: var(--fg-dim);
  text-align: center;
  padding: 2rem;
}
```

- [ ] **Step 2: Commit**

```bash
git add src/static/style.css
git commit -m "feat: add status modal CSS styles"
```

---

### Task 7: Wire status button click, fetch, and render in controls.js

**Files:**
- Modify: `src/static/controls.js`

- [ ] **Step 1: Add status modal DOM helpers and render function**

Add at the top of the IIFE, after `"use strict";` (after line 13):

```javascript
  const statusModal = document.getElementById("status-modal");
  const statusBody = document.getElementById("status-modal-body");

  function openStatusModal() {
    statusModal.setAttribute("aria-hidden", "false");
    statusModal.classList.add("open");
    statusBody.innerHTML =
      '<div class="status-loading"><div class="spinner"></div></div>';
    fetch("/api/reddit-feeds/status")
      .then((r) => r.json().then((d) => ({ ok: r.ok, status: r.status, data: d })))
      .then((result) => {
        if (!result.ok) {
          statusBody.innerHTML =
            '<div class="status-error">' +
            (result.data.detail || "Reddit Feeds API error") +
            "</div>";
          return;
        }
        renderStatus(result.data);
      })
      .catch(() => {
        statusBody.innerHTML =
          '<div class="status-error">Failed to reach status endpoint</div>';
      });
    collapseControls();
  }

  function renderStatus(data) {
    const feeds = data.feeds || [];
    if (feeds.length === 0) {
      statusBody.innerHTML =
        '<div class="status-empty">No feed data yet — first run hasn\'t completed</div>';
      return;
    }
    const rows = feeds
      .map(
        (f) =>
          "<tr>" +
          "<td>" + f.name + "</td>" +
          "<td><span class='status-dot " + f.last_status + "'></span>" + f.last_status + "</td>" +
          "<td>" + (f.last_fetch ? new Date(f.last_fetch).toLocaleString() : "—") + "</td>" +
          "<td>" + (f.last_item_count != null ? f.last_item_count : "—") + "</td>" +
          "<td>" + (f.total_items != null ? f.total_items : "—") + "</td>" +
          "</tr>"
      )
      .join("");
    statusBody.innerHTML =
      '<table class="status-table">' +
      "<thead><tr>" +
      "<th>Feed</th><th>Status</th><th>Last Fetch</th><th>Last Count</th><th>Total</th>" +
      "</tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "</table>" +
      (data.last_run
        ? '<div class="status-footer">Last run: ' + new Date(data.last_run).toLocaleString() + "</div>"
        : "");
  }

  function closeStatusModal() {
    statusModal.classList.remove("open");
    statusModal.setAttribute("aria-hidden", "true");
  }
```

- [ ] **Step 2: Wire the button, backdrop, ESC key, and close button in init()**

In the `init()` function, add after the existing `fab` click handler (after line 59):

```javascript
    document.getElementById("btn-status").addEventListener("click", () => {
      if (statusModal.classList.contains("open")) {
        closeStatusModal();
      } else {
        openStatusModal();
      }
    });
    document.getElementById("status-modal-close").addEventListener("click", closeStatusModal);
    statusModal.addEventListener("click", (e) => {
      if (e.target === statusModal) closeStatusModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && statusModal.classList.contains("open")) {
        closeStatusModal();
      }
    });
```

- [ ] **Step 3: Update mobile CSS for the new button (5th option)**

In `style.css`, after the `.option:nth-of-type(4)` rule (line 212), add:

```css
  #controls .option:nth-of-type(5) { left: calc(100% + 0.5rem + 3 * (var(--control-btn-size) + 0.5rem)); }
```

- [ ] **Step 4: Commit**

```bash
git add src/static/controls.js src/static/style.css
git commit -m "feat: wire status modal open/close and render"
```

---

### Task 8: Final verification

- [ ] **Step 1: Run linter**

```bash
uv run ruff check .
```

Expected: no errors.

- [ ] **Step 2: Run formatter**

```bash
uv run ruff format .
```

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest
```

Expected: all tests pass, coverage >= 90%.

- [ ] **Step 4: Commit any formatting changes**

```bash
git add -u
git commit -m "chore: ruff format"
```