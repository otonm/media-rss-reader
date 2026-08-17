# Media RSS Reader

A self-hosted media viewer that turns RSS feeds containing images, GIFs, or videos into a smooth, fullscreen browsing experience — like a private feed you control.

The backend continuously fetches feeds in the background (no browser session required), stores media items in SQLite, and serves a lightweight browser UI over HTTP. All configuration is done through environment variables; no external services, no build step.

## Features

- **Media-first** — only images, GIFs, and videos are shown; text content is ignored
- **Gallery support** — items with multiple images display as horizontally scrollable galleries with dot indicators, on-screen arrows, and arrow-key navigation
- **Scroll mode** — continuous vertical feed with keyboard/swipe navigation and auto-scroll
- **Auto-scroll** — per-item dwell timer: images advance after a configurable delay, GIFs after one full cycle, videos after play-through
- **Zoom to 100%** — double-tap, double-click or `z` scales an image to 1:1 and pans it with the cursor or a finger
- **Pre-fetch cache** — upcoming media is downloaded before you reach it, eliminating load stalls
- **Persistent storage** — feed items survive restarts; seen state is durable and survives pruning, feed removal and cross-posting
- **Deduplication** — the same picture posted to several feeds is stored once, by URL identity, by exact bytes, and optionally by perceptual similarity (catching re-uploads and re-encodes)
- **OPML-driven** — manage your feed list with any RSS reader's export format, or drop RSS `*.xml` files in a watched folder
- **Authentication** — username/password + TOTP (set up on first login), signed 7-day session cookies, IP-based brute-force lockout
- **Docker-native** — single container, volume-mounted data, no external database service
- **PWA-ready** — installable as a standalone app; service worker caches the UI for offline-capable loading
- **Dead-URL self-healing** — media URLs that are permanently gone are tracked; items whose media is entirely gone are automatically dropped and not re-inserted on the next feed poll
- **Reddit Feeds integration** — status modal showing companion service health (feed names, last fetch, item counts)

## Key Bindings

| Key | Action |
|---|---|
| `j` / `↓` | Next item |
| `k` / `↑` | Previous item |
| `←` / `→` | Previous / next gallery slide (steps items at the boundary) |
| `z` | Zoom the current image to 100% / back |
| `a` | Toggle auto-scroll |
| `m` | Toggle mute |
| `s` | Toggle show seen items |
| `Esc` | Close the status modal |

On mobile, swipe up/down to navigate and left/right within a gallery; double-tap
an image to zoom it to 100% and drag to pan. Tap ☰ to open the control menu. With
a mouse or pen, click-and-drag past ~40 px acts as a swipe.

## Prerequisites

- Docker ≥ 24.0
- Docker Compose v2 (`docker compose`, not `docker-compose`)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/yourname/media-rss-reader.git
cd media-rss-reader

# 2. Set required auth credentials in docker-compose.yml:
#    AUTH_USERNAME, AUTH_PASSWORD, AUTH_SECRET_KEY

# 3. Edit feeds.opml with your feed URLs, then start
docker compose up -d
```

Open http://localhost:8082 in your browser. You will be redirected to `/login`. On first login (before TOTP is configured), entering your username and password redirects to `/setup` where you scan the QR code or copy the secret into an authenticator app. Subsequent logins require all three fields: username, password, and TOTP code.

The first feed fetch runs immediately on startup; media appears within a few seconds after logging in.

## Feed Sources

Feeds come from two places, and both are used together:

1. **An [OPML](https://opml.org/) file** (`OPML_PATH`) listing remote feed URLs,
   which the reader fetches over HTTP.
2. **A watched folder** (`FEEDS_DIR`) of RSS `*.xml` files already on local disk —
   for feeds produced by a companion service on the same host. These are read
   directly, never fetched, and are re-parsed only when the file's modification
   time changes.

The two are reconciled into one feed list on every sync. If a folder file has the
same basename as an OPML entry, the folder wins. A feed that disappears from
**both** sources is deleted along with all its stored items — except when the
union comes back completely empty, which is treated as "the sources are
unreadable" (an unmounted volume, a companion mid-restart) rather than "delete
everything".

### OPML file

The same export format used by RSS readers like Feedly, NetNewsWire, and Reeder.

Create `feeds.opml` in the project directory (the default path the container mounts):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>My Feeds</title></head>
  <body>
    <outline type="rss" text="Hubble Images"
             xmlUrl="https://www.nasa.gov/rss/dyn/hubble_news.rss"/>
    <outline type="rss" text="Astronomy Picture of the Day"
             xmlUrl="https://apod.nasa.gov/apod.rss"/>
  </body>
</opml>
```

The file is re-read on the interval set by `OPML_SYNC_INTERVAL`. Adding or removing a feed takes effect on the next sync. Removing a feed cascades — all its stored items are deleted from the database.

A missing or malformed OPML file is logged and skipped, not fatal — useful if you
drive the reader from `FEEDS_DIR` alone.

## Configuration

All settings are environment variables configured directly in `docker-compose.yml`.

### Required

| Variable | Description |
|---|---|
| `AUTH_USERNAME` | Login username |
| `AUTH_PASSWORD` | Login password |
| `AUTH_SECRET_KEY` | Signs session cookies — rotate to invalidate all active sessions instantly |

Generate a suitable secret key with `openssl rand -hex 32`.

The container refuses to start if any of the three is missing. Empty credentials
are **not** a "no auth" mode — the first visitor would be handed the TOTP
enrollment flow and become the owner.

### Optional — paths

| Variable | Default | Description |
|---|---|---|
| `OPML_PATH` | `/data/feeds.opml` | Path to the OPML file inside the container. Empty string disables the OPML pass |
| `FEEDS_DIR` | `/feeds-output` | Directory scanned for `*.xml` RSS feeds already on local disk |
| `DB_PATH` | `/data/db/reader.db` | SQLite database path inside the container |
| `CACHE_DIR` | `/cache` | Directory for cached media files |

### Optional — schedule and retention

| Variable | Default | Description |
|---|---|---|
| `OPML_SYNC_INTERVAL` | `3600` | Seconds between feed-list reconciliations |
| `FEED_REFRESH_INTERVAL` | `900` | Seconds between feed content refresh cycles |
| `KEEP_ITEMS` | `1000` | Max items kept in the database |
| `ITEMS_MAX_AGE_HOURS` | `168` | Delete **seen** items older than this (168 = 7 days). Unseen items get 4× this budget, since you haven't had a chance to see them yet |
| `CACHE_MAX_ITEMS` | `500` | Max number of media files kept on disk |
| `CACHE_MAX_AGE_HOURS` | `48` | Max age of cached files before eviction |
| `CACHE_MAX_BYTES` | `2147483648` | Total cache size budget in bytes (2 GiB). `0` disables. A file count alone cannot bound a directory of multi-gigabyte videos |
| `MEDIA_MAX_BYTES` | `268435456` | Largest single media transfer in bytes (256 MiB). `0` disables. Both the declared `Content-Length` and the running total are checked |

### Optional — behaviour

| Variable | Default | Description |
|---|---|---|
| `PREFETCH_AHEAD` | `5` | Items to pre-fetch ahead of the current scroll position |
| `FEED_INITIAL_COUNT` | `10` | Items per API page and browser lookahead. Must be 1–200; the container refuses to start outside that range |
| `IMAGE_AUTOSCROLL_DELAY_S` | `2` | Dwell time per image in auto-scroll (seconds); also the **minimum** dwell for GIFs and videos |
| `MEDIA_LOAD_TIMEOUT_S` | `10` | How long the browser waits for a media download before giving up. **A timeout deletes the item**, so this erases posts that were merely slow — raise it if usable posts start disappearing. Must be 1–300 |
| `ZOOM_TRANSITION_MS` | `200` | How long the zoom-to-100% gesture animates (milliseconds); `0` snaps instantly. Panning is never animated, and `prefers-reduced-motion` overrides this to `0` |
| `DEDUP_SIMILARITY` | `97` | Perceptual-hash threshold as a percentage of matching bits. `0` disables perceptual matching (URL-key and exact-byte dedup always run). 97 drops an image whose 256-bit hash differs by ≤5 bits |
| `ALLOW_PRIVATE_MEDIA_HOSTS` | `0` | `1` lets media URLs point at loopback/RFC1918 addresses. Off by default: media URLs come from third-party feed content and are fetched with no session at all. Turn it on only if you serve media from another container on the same Docker network |
| `UI_DEBUG` | `0` | Set to `1` to show a diagnostic overlay in the top-right corner: the current item's feed, title, media type, slide count, publish date, cache hit/miss with load time, and the download queue depth |

### Optional — server and integrations

| Variable | Default | Description |
|---|---|---|
| `AUTH_LOCKOUT_ATTEMPTS` | `5` | Failed login attempts before IP lockout |
| `AUTH_LOCKOUT_MINUTES` | `15` | Lockout duration in minutes |
| `PORT` | `8080` | Port the server listens on inside the container (host port is set by the `-p` flag in Docker / Compose) |
| `LOG_LEVEL` | `info` | Log level: `debug` \| `info` \| `warning` \| `error` |
| `REDDIT_FEEDS_API_URL` | `http://127.0.0.1:9090` | URL of the Reddit Feeds status API (for the status modal) |

## Deployment: Docker Only

Use this if you prefer plain `docker run` without Compose.

```bash
# Create named volumes for data persistence
docker volume create media-rss-data
docker volume create media-rss-cache

# Run the container
docker run -d \
  --name media-rss \
  --restart unless-stopped \
  -p 8082:8080 \
  -v ./feeds.opml:/data/feeds.opml:ro \
  -v media-rss-data:/data/db \
  -v media-rss-cache:/cache \
  -e TZ=Europe/Berlin \
  -e AUTH_USERNAME=admin \
  -e AUTH_PASSWORD=yourpassword \
  -e AUTH_SECRET_KEY=$(openssl rand -hex 32) \
  ghcr.io/otonm/media-rss-reader:latest
```

- `-v ./feeds.opml:/data/feeds.opml:ro` — mounts your local OPML file read-only into the container
- `-v media-rss-data:/data/db` — persists the SQLite database across container restarts
- `-v media-rss-cache:/cache` — persists the media disk cache across restarts
- Add `-e VAR=value` for any settings you want to override (see [Configuration](#configuration))

## Deployment: Docker Compose

The included `docker-compose.yml` wires everything up:

```yaml
services:
  media-rss:
    image: ghcr.io/otonm/media-rss-reader:latest
    ports:
      - "8082:8080"           # host:container — change 8082 to your preferred port
    volumes:
      - ./feeds.opml:/data/feeds.opml:ro   # OPML feed list (read-only)
      - reader_data:/data/db               # SQLite database
      - media_cache:/cache                 # media disk cache
    environment:
      - TZ=Europe/Berlin
      - AUTH_USERNAME=admin
      - AUTH_PASSWORD=changeme
      - AUTH_SECRET_KEY=replace-with-a-random-32-char-secret
      # - LOG_LEVEL=debug
      # - FEED_INITIAL_COUNT=10
      # - IMAGE_AUTOSCROLL_DELAY_S=2
      # - ZOOM_TRANSITION_MS=200
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:8080/health || exit 1"]
      interval: 30s
      timeout: 5s
      start_period: 60s
      retries: 3

volumes:
  reader_data:   # survives docker compose down
  media_cache:
```

```bash
docker compose up -d          # start in background
docker compose logs -f        # follow logs
docker compose down           # stop (volumes are preserved)
docker compose down -v        # stop AND delete all data
```

## Deployment: Cloudflare Tunnel

This setup exposes the reader to the internet without opening firewall ports. The app's built-in authentication (username + password + TOTP) handles access control; Cloudflare acts purely as a TLS-terminating reverse proxy that sets the `X-Forwarded-Proto: https` header the app requires.

You can additionally enable Cloudflare Access as a second authentication layer (see Step 4), but it is optional.

Everything runs inside Docker — no `cloudflared` binary needs to be installed on your machine.

**What you need:**
- A domain managed by Cloudflare (free account is sufficient)
- A Cloudflare Zero Trust account (free tier covers personal use; visit [one.dash.cloudflare.com](https://one.dash.cloudflare.com))

---

### Step 1: Create a Tunnel in the Cloudflare Dashboard

1. Go to [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → **Zero Trust** → **Networks** → **Tunnels**
2. Click **Create a tunnel**
3. Choose **Cloudflared** as the connector type
4. Name the tunnel (e.g. `media-reader`) and click **Save tunnel**
5. On the next screen, select **Docker** in the connector instructions — you will see a `docker run` command containing a `--token` value
6. Copy that token — you will paste it directly into `docker-compose.yml` in Step 3.

Leave the browser tab open — you will configure the public hostname in the next step.

---

### Step 2: Configure DNS

Still on the tunnel configuration page, open the **Public Hostname** tab:

1. Click **Add a public hostname**
2. Fill in:
   - **Subdomain**: `reader`
   - **Domain**: `example.com` (your domain)
   - **Service type**: `HTTP`
   - **URL**: `media-rss:8080` (the Docker Compose service name and port)
3. Click **Save hostname**

Cloudflare automatically creates the CNAME record in your DNS — no manual DNS editing required.

---

### Step 3: Add cloudflared as a Docker Compose Sidecar

Use this `docker-compose.yml` (note: the `ports:` mapping on `media-rss` is removed — all traffic arrives through the tunnel):

```yaml
services:
  media-rss:
    image: ghcr.io/otonm/media-rss-reader:latest
    # No host port binding — cloudflared connects to the container directly
    volumes:
      - ./feeds.opml:/data/feeds.opml:ro
      - reader_data:/data/db
      - media_cache:/cache
    environment:
      - TZ=Europe/Berlin
      - AUTH_USERNAME=admin
      - AUTH_PASSWORD=changeme
      - AUTH_SECRET_KEY=replace-with-a-random-32-char-secret
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:8080/health || exit 1"]
      interval: 30s
      timeout: 5s
      start_period: 60s
      retries: 3

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=eyJhI...   # paste your tunnel token here
    depends_on:
      - media-rss
    restart: unless-stopped

volumes:
  reader_data:
  media_cache:
```

Start both services:

```bash
docker compose up -d
docker compose logs cloudflared   # should show "Registered tunnel connection"
```

Visit `https://reader.example.com` — the app's built-in login page is displayed. The Cloudflare tunnel terminates TLS and sets `X-Forwarded-Proto: https`, which the app requires. Continue to Step 4 to optionally add Cloudflare Access as an additional authentication layer.

---

### Step 4: (Optional) Enable Cloudflare Access as a Second Factor

This adds a Cloudflare-managed login page in front of the tunnel as an additional authentication layer. The app's own username/password/TOTP login still applies after passing this gate.

1. Go to **Zero Trust** → **Access** → **Applications** → **Add an application**
2. Choose **Self-hosted**
3. Fill in the application details:
   - **Application name**: `Media RSS Reader`
   - **Subdomain**: `reader`
   - **Domain**: `example.com` (your domain)
   - Leave **Session duration** at `24 hours`
4. Click **Next**
5. Under **Policies**, create a new policy:
   - **Policy name**: `Owner`
   - **Action**: `Allow`
   - **Configure rules → Include**: selector `Emails`, value `your@email.com`
6. Click **Next**, then **Add application**

Now visiting `https://reader.example.com` shows a Cloudflare login page. Enter your email address, receive a one-time code, and get a 24-hour session. No password or account setup required on your side.

**Optional: bypass the login from your home network**

Add a second Include rule to the policy:
- Selector: `IP ranges`
- Value: your home IP address or CIDR (e.g. `203.0.113.0/24`)

Requests from that IP range bypass the email check entirely.

---

## Updating

```bash
git pull
docker compose pull
docker compose up -d
```

Schema migrations run automatically on startup — no manual steps required.

## Documentation

| Document | For |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Working on this codebase: module layout, data flows, and the reasoning behind each load-bearing decision |
| [spec.md](spec.md) | Reimplementing the reader elsewhere: a language- and library-agnostic description of the data model, algorithms and invariants, with porting notes and an acceptance checklist |

## Development

```bash
uv sync --extra dev                              # install, including dev extras
uv run uvicorn src.main:app --reload --port 8080 # run locally
uv run ruff check --fix . && uv run ruff format . # lint + format
uv run pytest                                    # tests + coverage (90% floor)
```

Running locally still requires `AUTH_USERNAME`, `AUTH_PASSWORD` and
`AUTH_SECRET_KEY` in the environment, and every request needs an
`X-Forwarded-Proto: https` header — the app assumes a TLS-terminating reverse
proxy in front of it and rejects anything else with a 403.
