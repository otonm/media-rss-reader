import asyncio
import hashlib
import time
from pathlib import Path

from src.config import settings


def _cache_path(url: str) -> Path:
    return Path(settings.cache_dir) / hashlib.sha256(url.encode()).hexdigest()


async def cache_write(url: str, data: bytes) -> Path:
    path = _cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    return path


def cache_read(url: str) -> Path | None:
    path = _cache_path(url)
    return path if path.exists() else None


async def evict() -> None:
    cache_dir = Path(settings.cache_dir)
    if not cache_dir.exists():  # noqa: ASYNC240
        return
    now = time.time()
    max_age_secs = settings.cache_max_age_hours * 3600

    files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)  # noqa: ASYNC240
    surviving: list[Path] = []
    for f in files:
        if now - f.stat().st_mtime > max_age_secs:
            f.unlink(missing_ok=True)
        else:
            surviving.append(f)

    while len(surviving) > settings.cache_max_items:
        surviving.pop(0).unlink(missing_ok=True)
