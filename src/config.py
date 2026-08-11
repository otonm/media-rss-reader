"""Application configuration.

All settings are read from environment variables (uppercase names).
The settings singleton is imported directly by modules that need config
values at call time. Frontend-visible values are injected into the HTML
as CSS custom properties by main._build_html().
"""

import os
from dataclasses import dataclass, fields


@dataclass
class Settings:
    # --- Paths ---
    opml_path: str = "/data/feeds.opml"
    feeds_dir: str = "/feeds-output"
    db_path: str = "/data/db/reader.db"

    # --- Feed refresh schedule ---
    opml_sync_interval: int = 3600
    feed_refresh_interval: int = 900

    # --- Media cache ---
    cache_dir: str = "/cache"
    cache_max_items: int = 500
    cache_max_age_hours: int = 48
    # Largest single media transfer, in bytes (256 MiB). 0 disables the check.
    # Both the declared Content-Length and the running total are checked, so a
    # slow-drip server never trips the per-operation timeout.
    media_max_bytes: int = 268435456
    # Total cache size budget in bytes (2 GiB). 0 disables. Eviction by file
    # count alone cannot bound a directory of multi-gigabyte videos.
    cache_max_bytes: int = 2147483648
    # 1 lets media URLs point at loopback/RFC1918 addresses. Off by default:
    # media URLs come from third-party feed content and the prefetcher fetches
    # them with no session at all. Turn it on only if you serve media from
    # another container on the same Docker network. An int, not a bool, because
    # _load_settings only parses int and str.
    allow_private_media_hosts: int = 0

    # --- Item retention ---
    keep_items: int = 1000
    items_max_age_hours: int = 168
    prefetch_ahead: int = 5

    # --- Deduplication ---
    # Perceptual-hash similarity threshold, as a percentage of matching bits.
    # 0 disables perceptual matching entirely (URL-key and exact-byte dedup
    # always run). 97 is a sensible starting point: it drops an image whose
    # 256-bit hash differs by 5 bits or fewer. An int, not a bool, because
    # _load_settings only parses int and str.
    dedup_similarity: int = 97

    # --- WebUI behaviour (injected as CSS variables at startup) ---
    feed_initial_count: int = 10
    image_autoscroll_delay_s: int = 2
    # How long the browser waits for a media download before giving up. Not just
    # a UI nicety: a timeout reports the URL to /api/media/failed, which marks it
    # dead and deletes the item. Set below the server's UPSTREAM_TIMEOUT_S (30)
    # this erases posts that were merely slow — raise it if usable posts start
    # disappearing.
    media_load_timeout_s: int = 10
    # How long the zoom-to-100% gesture takes to animate, in milliseconds.
    # 0 snaps instantly. Panning is never animated regardless — the picture
    # follows the cursor, and a transition there would only lag behind it.
    # prefers-reduced-motion overrides this to 0 in the browser.
    zoom_transition_ms: int = 200
    # 1 shows a diagnostic overlay in the top-right corner naming the current
    # item and how it loaded. An int, not a bool, because _load_settings only
    # parses int and str.
    ui_debug: int = 0

    # --- Server ---
    port: int = 8080
    log_level: str = "info"

    # --- Authentication ---
    auth_username: str = ""
    auth_password: str = ""
    auth_secret_key: str = ""
    auth_lockout_attempts: int = 5
    auth_lockout_minutes: int = 15

    # --- Reddit Feeds integration ---
    reddit_feeds_api_url: str = "http://127.0.0.1:9090"


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
    # Both-empty is NOT a safe "no-auth mode": /login then accepts empty creds,
    # redirects to /setup with a setup cookie, and any visitor becomes admin.
    if not s.auth_secret_key:
        raise RuntimeError("AUTH_SECRET_KEY must be set; the session signer must not be empty")
    if not (s.auth_username and s.auth_password):
        raise RuntimeError(
            "AUTH_USERNAME and AUTH_PASSWORD must both be set; empty credentials "
            "are not safe with the /setup flow (any visitor becomes admin)"
        )
    # The browser sends size=FEED_INITIAL_COUNT to /api/items, which caps size
    # at 200. Above that every request 422s before the handler runs, so the feed
    # renders empty and retries forever with nothing in the application log.
    # Fail here rather than clamp: the operator asked for a page the API cannot
    # serve, and a silent clamp is how the two bounds drifted apart.
    if not 1 <= s.feed_initial_count <= 200:
        raise RuntimeError(f"FEED_INITIAL_COUNT must be between 1 and 200 (got {s.feed_initial_count})")
    # A timeout deletes the item it fires on, so a 0 or negative value would
    # empty the library on first scroll. Fail rather than clamp, as above.
    if not 1 <= s.media_load_timeout_s <= 300:
        raise RuntimeError(f"MEDIA_LOAD_TIMEOUT_S must be between 1 and 300 (got {s.media_load_timeout_s})")
    return s


settings = _load_settings()
