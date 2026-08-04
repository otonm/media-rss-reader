from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.scheduler as sched_mod
from src.db.connection import open_db
from src.db.migrations import run_migrations
from src.db.schema import create_schema


async def test_start_and_stop_scheduler(tmp_path: Path) -> None:
    """Test that the scheduler starts and stops cleanly."""
    opml_file = tmp_path / "feeds.opml"
    opml_file.write_text('<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>')

    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)

    with patch.object(sched_mod.settings, "opml_path", str(opml_file)):
        await sched_mod.start_scheduler(conn)
        assert sched_mod.get_http_client() is not None
        await sched_mod.stop_scheduler()

    with pytest.raises(RuntimeError):
        sched_mod.get_http_client()

    await conn.close()


async def test_stop_scheduler_noop_when_not_started() -> None:
    """stop_scheduler should not raise if called when already stopped."""
    sched_mod._state.scheduler = []
    sched_mod._state.client = None
    await sched_mod.stop_scheduler()


async def test_start_scheduler_creates_background_tasks(tmp_path: Path) -> None:
    """start_scheduler creates asyncio tasks for the sync loops."""
    opml_file = tmp_path / "feeds.opml"
    opml_file.write_text('<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>')

    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)

    with (
        patch("src.scheduler.httpx.AsyncClient", return_value=MagicMock(aclose=AsyncMock())),
        patch("src.scheduler.sync_feeds", new=AsyncMock()),
        patch("src.scheduler.refresh_all_feeds", new=AsyncMock()),
    ):
        await sched_mod.start_scheduler(conn)
        assert len(sched_mod._state.scheduler) == 3
        assert all(not t.done() for t in sched_mod._state.scheduler)
        await sched_mod.stop_scheduler()

    await conn.close()


async def test_stop_scheduler_cancels_warm_tasks() -> None:
    """R6: warm tasks lived in prefetch._bg_tasks and kept running against the
    closed HTTP client after stop_scheduler, their failures reduced to a debug
    line."""
    import asyncio

    import src.media.prefetch as prefetch_mod
    from src.scheduler import stop_scheduler

    async def _never() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    prefetch_mod._track(task)

    await stop_scheduler()

    assert task.cancelled() or task.done()
    assert not prefetch_mod._bg_tasks
