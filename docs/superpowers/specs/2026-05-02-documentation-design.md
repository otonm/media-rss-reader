---
title: Documentation — README, ARCHITECTURE, codebase comments
date: 2026-05-02
status: approved
---

## Goal

Produce three documentation artefacts for the media-rss-reader project:

1. **README.md** — end-user deployment guide (root of repo)
2. **ARCHITECTURE.md** — developer reference (root of repo)
3. **Codebase comments** — inline comments across all Python source files and app.js

---

## README.md

Target audience: someone deploying this for personal use who knows Docker but has not read the source.

Sections (in order):
1. One-paragraph description + features bullet list
2. Prerequisites (Docker ≥ 24, docker-compose v2)
3. Quick start — three commands: clone / copy .env.example / docker compose up
4. OPML file format — what it is, minimal example, where to put it
5. Configuration reference — full env-var table (name, default, description)
6. Deployment: Docker only — `docker run` command with all volumes and env-file flags explained
7. Deployment: Docker Compose — annotated compose file walkthrough
8. Deployment: Cloudflared
   a. Install cloudflared and log in (`cloudflared tunnel login`)
   b. Create a named tunnel (`cloudflared tunnel create media-reader`)
   c. Write `cloudflared.yml` ingress config pointing to the container
   d. Add cloudflared as a sidecar service in docker-compose.yml
   e. Cloudflare Access (Zero Trust) — create application, add email-based policy, bind to tunnel hostname
9. Key bindings cheat sheet (table)
10. Updating (pull + rebuild)

---

## ARCHITECTURE.md

Target audience: developer reading the code for the first time.

Sections:
1. System overview — ASCII diagram showing the three planes: browser / FastAPI / background scheduler
2. Directory map — annotated tree
3. Startup sequence — lifespan hook step-by-step
4. Background scheduler — two APScheduler jobs, eager-fire on startup, `_State` singleton
5. Feed pipeline — OPML parse → HTTP fetch → feedparser → media detection priority chain (enclosure → media:content → media:thumbnail → og:image)
6. Database — schema rationale, WAL mode, per-request vs. persistent connection, integer migration counter
7. Media subsystem — cache write/read/evict policy, prefetch-ahead query, startup warmup semaphore, streaming proxy
8. API layer — router layout, interleaved pagination query, seen-tracking flow
9. Frontend — module sections (state, constants, observers, navigation, view modes), three IntersectionObserver roles, CSS-variable injection pattern, RAF auto-scroll loop, GIF duration byte-scan
10. Configuration — pydantic-settings env-var binding, how values reach the frontend via `_build_html()`
11. Testing — fixture strategy, coverage target

---

## Codebase comments

Rules:
- **Python**: one-line module docstring per file; short docstring on non-trivial functions; `#` inline comments before logic blocks whose purpose is not obvious from reading the code
- **app.js**: explanatory comments inside `mediaObserver` callback (pause/resume logic), `getGifDuration` byte-scan loop, `fetchItems` stale-generation guard, `seenObserver` exit-via-top detection
- **style.css**: no changes needed — sections are already well-labelled

Files to touch:
- `src/config.py`
- `src/main.py`
- `src/scheduler.py`
- `src/db/connection.py`, `schema.py`, `migrations.py`
- `src/feeds/opml.py`, `fetcher.py`, `sync.py`
- `src/media/detector.py`, `cache.py`, `prefetch.py`
- `src/api/feeds.py`, `items.py`, `media.py`
- `src/static/app.js`
