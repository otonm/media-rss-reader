# Reddit Feeds Status Page

A modal overlay accessible from the hamburger menu that displays the operational status of
the [Reddit Feeds](https://github.com/otonm/reddit-feeds) companion service.

## Motivation

The user runs a Reddit Feeds daemon alongside this RSS reader. They want to check its health
without leaving the Media RSS Reader UI.

## Design

### Architecture

```
Browser                       Backend                     Reddit Feeds
  │                              │                            │
  ├── click 📊 button            │                            │
  ├── fetch("/api/reddit-feeds/status")
  │                              ├── httpx.get(URL + "/status")
  │                              │                            ├── return JSON
  │                              ├── return JSON              │
  ├── render table + last_run    │                            │
```

Backend proxies the Reddit Feeds API to avoid CORS. The external URL is server-side only.

### Files

| File | Action |
|------|--------|
| `src/config.py` | Add `reddit_feeds_api_url: str = "http://127.0.0.1:9090"` |
| `src/api/reddit_feeds.py` | **New** — single route `GET /api/reddit-feeds/status` |
| `src/main.py` | Register `reddit_feeds.router` under `/api` |
| `src/static/index.html` | Add 📊 button to controls, add `<div id="status-modal">` |
| `src/static/style.css` | Modal overlay + status table styles |
| `src/static/controls.js` | Wire button → fetch → render |

### Backend route

```
GET /api/reddit-feeds/status

Success (200): Reddit Feeds JSON as-is
Error (502):  {"error": "Reddit Feeds API unreachable"}
Error (upstream): {"error": "<upstream message>"} with matching status code
```

Uses `httpx.AsyncClient` (already a dependency). No caching needed — no auto-refresh.

### Modal states

| State | UI |
|-------|-----|
| Loading | Spinner (reuse `--spinner-size`, `--spinner-track`, `--spinner-head` CSS vars) |
| Empty | "No feed data yet — first run hasn't completed" |
| Populated | Table: name, status (green/red dot), last_fetch, last_item_count, total_items + footer: last_run |
| Error | Red-tinted error message |

### Dismissal

- Click × close button (top-right)
- Click backdrop (outside modal card)
- Press Escape key

### Styling

- Modal: fixed overlay, `rgba(0,0,0,0.7)` backdrop, centered card, max-width 600px
- Table: striped rows using existing `--bg`/`--fg`/`--accent` CSS variables
- Success: green dot (`#4caf50`), Error: red dot (`#f44336`)
- Timestamps formatted via `new Date(ts).toLocaleString()`
- Buttons match existing control button style

### Testing

One test case added to existing test pattern:

1. Mock Reddit Feeds returns valid status JSON → verify 200 + correct forwarding
2. Mock Reddit Feeds connection refused → verify 502 + error message
3. Mock Reddit Feeds returns 500 → verify upstream error with correct status code