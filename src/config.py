"""Application configuration.

All settings are read from environment variables (uppercase names).
pydantic-settings handles the env-var binding automatically — no .env
file parsing occurs at this level; that is handled by Docker / the shell.

The settings singleton is imported directly by modules that need config
values at call time. Frontend-visible values are injected into the HTML
as CSS custom properties by main._build_html().
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Paths ---
    opml_path: str = "/data/feeds.opml"  # OPML file inside the container
    db_path: str = "/data/db/reader.db"  # SQLite database file

    # --- Feed refresh schedule ---
    opml_sync_interval: int = 3600  # seconds between OPML re-reads
    feed_refresh_interval: int = 900  # seconds between feed refresh cycles

    # --- Media cache ---
    cache_dir: str = "/cache"
    cache_max_items: int = 500  # max files on disk
    cache_max_age_hours: int = 48  # evict files older than this

    # --- Item retention ---
    prefetch_ahead: int = 5  # items to pre-warm ahead of scroll
    keep_items: int = 1000  # max rows in the items table
    items_max_age_hours: int = 168  # delete seen items older than 7 days

    # --- Frontend behaviour (injected as CSS variables at startup) ---
    image_display_delay_ms: int = 5000  # dwell time per image/GIF in auto-scroll (ms)
    slideshow_transition_ms: int = 400  # CSS crossfade duration (ms)
    auto_scroll_speed: float = 1.5  # px per animation frame (~90px/s at 60fps)

    # --- Server ---
    port: int = 8080
    log_level: str = "info"  # uvicorn log level

    # --- Authentication ---
    auth_username: str
    auth_password: SecretStr
    auth_secret_key: SecretStr
    auth_lockout_attempts: int = 5
    auth_lockout_minutes: int = 15


settings = Settings()
