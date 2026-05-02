# Media RSS Reader

A self-hosted media viewer that turns RSS feeds containing images, GIFs, or videos into a smooth, fullscreen browsing experience — like a private feed you control.

The backend continuously fetches feeds in the background (no browser session required), stores media items in SQLite, and serves a lightweight browser UI over HTTP. All configuration is done through environment variables; no accounts, no external services, no build step.

## Features

- **Media-first** — only images, GIFs, and videos are shown; text content is ignored
- **Scroll mode** — continuous vertical feed with keyboard/swipe navigation and auto-scroll
- **Slideshow mode** — fullscreen single-item view with CSS crossfade transitions
- **Dark / light theme** — toggle with `d`, persisted across sessions
- **Auto-scroll** — continuous pixel-level drift; pauses automatically for videos and GIFs
- **Pre-fetch cache** — upcoming media is downloaded before you reach it, eliminating load stalls
- **Persistent storage** — feed items survive restarts; seen state tracked per item
- **OPML-driven** — manage your feed list with any RSS reader's export format
- **Docker-native** — single container, volume-mounted data, no external database service

## Prerequisites

- Docker ≥ 24.0
- Docker Compose v2 (`docker compose`, not `docker-compose`)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/yourname/media-rss-reader.git
cd media-rss-reader

# 2. Copy and edit the environment file
cp .env.example .env
$EDITOR .env        # defaults work for most setups

# 3. Edit feeds.opml with your feed URLs, then start
docker compose up -d
```

Open http://localhost:8082 in your browser. The first fetch runs immediately on startup; media appears within a few seconds.

## OPML Feed List

The reader is driven by an [OPML](https://opml.org/) file — the same export format used by RSS readers like Feedly, NetNewsWire, and Reeder.

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

## Configuration

All settings are environment variables. Copy `.env.example` to `.env` and adjust as needed. Defaults work for most setups.

| Variable | Default | Description |
|---|---|---|
| `OPML_PATH` | `/data/feeds.opml` | Path to the OPML file inside the container |
| `DB_PATH` | `/data/db/reader.db` | SQLite database path inside the container |
| `CACHE_DIR` | `/cache` | Directory for cached media files |
| `OPML_SYNC_INTERVAL` | `3600` | Seconds between OPML re-reads |
| `FEED_REFRESH_INTERVAL` | `900` | Seconds between feed refresh cycles |
| `CACHE_MAX_ITEMS` | `500` | Max number of media files kept on disk |
| `CACHE_MAX_AGE_HOURS` | `48` | Max age of cached files before eviction |
| `KEEP_ITEMS` | `1000` | Max items kept in the database |
| `ITEMS_MAX_AGE_HOURS` | `168` | Delete seen items older than this (hours; 168 = 7 days) |
| `PREFETCH_AHEAD` | `5` | Items to pre-fetch ahead of current scroll position |
| `IMAGE_DISPLAY_DELAY_MS` | `5000` | Dwell time per image/GIF in auto-scroll / slideshow (ms) |
| `SLIDESHOW_TRANSITION_MS` | `400` | CSS crossfade duration between slideshow items (ms) |
| `AUTO_SCROLL_SPEED` | `1.5` | Pixels scrolled per animation frame (~90 px/s at 60 fps) |
| `PORT` | `8080` | Port the server listens on inside the container (host port is set by the `-p` flag in Docker / Compose) |
| `LOG_LEVEL` | `info` | Uvicorn log level: `debug` \| `info` \| `warning` \| `error` |

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
  --env-file .env \
  -e TZ=Europe/Berlin \         # set to your timezone, e.g. America/New_York
  ghcr.io/otonm/media-rss-reader:latest
```

- `-v ./feeds.opml:/data/feeds.opml:ro` — mounts your local OPML file read-only into the container
- `-v media-rss-data:/data/db` — persists the SQLite database across container restarts
- `-v media-rss-cache:/cache` — persists the media disk cache across restarts
- `--env-file .env` — loads all configuration from your `.env` file

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
    env_file:
      - .env                  # load all variables from .env
    environment:
      - TZ=Europe/Berlin      # timezone
    restart: unless-stopped

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

## Deployment: Cloudflare Tunnel + Access

This setup exposes the reader securely to the internet without opening firewall ports, and locks it behind Cloudflare Access email authentication so only authorised users can reach it.

**What you need:**
- A domain managed by Cloudflare (free account is sufficient)
- A Cloudflare Zero Trust account (free tier covers personal use; visit [one.dash.cloudflare.com](https://one.dash.cloudflare.com))

---

### Step 1: Create a Cloudflare Tunnel

Install `cloudflared` on your Docker host or local machine:

```bash
# Debian / Ubuntu
curl -L https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared bookworm main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# macOS
brew install cloudflare/cloudflare/cloudflared
```

Log in and create a named tunnel:

```bash
cloudflared tunnel login              # opens browser — authorise in Cloudflare dashboard
cloudflared tunnel create media-reader  # creates tunnel; note the Tunnel ID in the output
```

---

### Step 2: Get a Tunnel Token

The easiest Docker deployment uses a single token rather than a credentials file:

1. Go to [one.dash.cloudflare.com](https://one.dash.cloudflare.com) → **Zero Trust** → **Networks** → **Tunnels**
2. Click the tunnel named `media-reader`
3. Open the **Configure** tab → select **Docker** in the connector instructions
4. Copy the `--token` value shown

Add it to your `.env` file:

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJhI...   # paste the full token here
```

---

### Step 3: Configure DNS

Point a subdomain at the tunnel. Either use the CLI:

```bash
cloudflared tunnel route dns media-reader reader.example.com
```

Or add it manually in the Cloudflare DNS dashboard:

| Type | Name | Target | Proxy |
|------|------|--------|-------|
| CNAME | `reader` | `<TUNNEL-ID>.cfargotunnel.com` | Proxied (orange cloud) |

Replace `<TUNNEL-ID>` with the ID printed during tunnel creation and `example.com` with your domain.

---

### Step 4: Add cloudflared as a Docker Compose Sidecar

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
    env_file: .env
    environment:
      - TZ=Europe/Berlin
    restart: unless-stopped

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
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

Visit `https://reader.example.com` — the app is accessible (unauthenticated at this point). Continue to Step 5 to add the login gate.

---

### Step 5: Enable Cloudflare Access Authentication

This adds a login page in front of the tunnel. Only users whose email address matches the policy can get in.

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

## Key Bindings

| Key | Action |
|---|---|
| `j` / `↓` | Next item |
| `k` / `↑` | Previous item |
| `a` | Toggle auto-scroll |
| `s` | Toggle slideshow mode |
| `m` | Toggle mute |
| `d` | Toggle dark / light theme |

On mobile, swipe up/down to navigate. Tap ☰ to open the control menu.

## Updating

```bash
git pull
docker compose pull
docker compose up -d
```

Schema migrations run automatically on startup — no manual steps required.
