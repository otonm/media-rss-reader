from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    opml_path: str = "/data/feeds.opml"
    db_path: str = "/data/db/reader.db"
    opml_sync_interval: int = 3600
    feed_refresh_interval: int = 900
    cache_dir: str = "/cache"
    cache_max_items: int = 500
    cache_max_age_hours: int = 48
    prefetch_ahead: int = 5
    image_display_delay_ms: int = 5000
    slideshow_transition_ms: int = 400
    keep_items: int = 1000
    items_max_age_hours: int = 168  # 7 days
    port: int = 8080
    log_level: str = "info"


settings = Settings()
