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
    # Both the declared Content-Length and the running total are checked: a
    # slow-drip server never trips the per-operation timeout (R7).
    media_max_bytes: int = 268435456
    # Total cache size budget in bytes (2 GiB). 0 disables. Eviction by file
    # count alone cannot bound a directory of multi-gigabyte videos.
    cache_max_bytes: int = 2147483648
    # 1 lets media URLs point at loopback/RFC1918 addresses. Off by default:
    # media URLs come from third-party feed content and the prefetcher fetches
    # them with no session at all (R1). Turn it on only if you serve media from
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
    dedup_similarity: int = 0

    # --- WebUI behaviour (injected as CSS variables at startup) ---
    feed_initial_count: int = 10
    image_autoscroll_delay_s: int = 2
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
    return s


settings = _load_settings()
