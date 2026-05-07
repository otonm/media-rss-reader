import asyncio
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
    opml_file.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>'
    )

    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)

    with patch.object(sched_mod.settings, "opml_path", str(opml_file)):
        await sched_mod.start_scheduler(conn)
        assert sched_mod.get_http_client() is not None
        await sched_mod.stop_scheduler()

    # After stop, client should be gone
    with pytest.raises(RuntimeError):
        sched_mod.get_http_client()

    await conn.close()


async def test_get_last_opml_sync_none_before_start() -> None:
    """get_last_opml_sync returns None when scheduler has not synced yet."""
    sched_mod._state.last_opml_sync = None
    assert sched_mod.get_last_opml_sync() is None


async def test_stop_scheduler_noop_when_not_started() -> None:
    """stop_scheduler should not raise if called when already stopped."""
    sched_mod._state.scheduler = None
    sched_mod._state.client = None
    await sched_mod.stop_scheduler()


async def test_start_scheduler_sets_last_opml_sync(tmp_path: Path) -> None:
    """After start_scheduler, _last_opml_sync is set when OPML sync succeeds."""
    opml_file = tmp_path / "feeds.opml"
    opml_file.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>'
    )

    conn = await open_db(":memory:")
    await create_schema(conn)
    await run_migrations(conn)

    sched_mod._state.last_opml_sync = None

    with patch.object(sched_mod.settings, "opml_path", str(opml_file)):
        await sched_mod.start_scheduler(conn)
        # start_scheduler now returns immediately; yield to the event loop so the
        # background _startup_sync task can run and set last_opml_sync.
        await asyncio.sleep(0.1)
        sync_time = sched_mod.get_last_opml_sync()
        await sched_mod.stop_scheduler()

    assert sync_time is not None

    await conn.close()


async def test_start_scheduler_passes_running_loop() -> None:
    """AsyncIOScheduler must receive the running event loop to fire jobs in FastAPI."""
    mock_instance = MagicMock()
    mock_instance.start = MagicMock()
    mock_instance.add_job = MagicMock()

    with (
        patch("src.scheduler.AsyncIOScheduler", return_value=mock_instance) as mock_cls,
        patch("src.scheduler.httpx.AsyncClient"),
        patch("src.scheduler.opml_sync", new=AsyncMock()),
        patch("src.scheduler.refresh_all_feeds", new=AsyncMock()),
    ):
        mock_db = MagicMock()
        await sched_mod.start_scheduler(mock_db)

        loop = asyncio.get_running_loop()
        mock_cls.assert_called_once_with(event_loop=loop)
