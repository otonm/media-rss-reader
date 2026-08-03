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
    return Settings(**kwargs)


settings = _load_settings()
