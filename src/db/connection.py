"""Database connection factory.

open_db() opens a connection held for the process lifetime. src/main.py opens three:
one the requests share via get_db(), one the scheduler keeps to itself, one
installed via set_writer_db() for post-response writers.
get_db() is a FastAPI dependency returning the request-side connection.
run_with_own_db() is for work that outlives the request that started it.

DbDep is the annotated dependency four modules in two other packages import;
it is deliberately public.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated

import aiosqlite
from fastapi import Depends, Request

from src.config import settings

logger = logging.getLogger(__name__)


async def open_db(path: str | None = None) -> aiosqlite.Connection:
    """Open an aiosqlite connection with WAL mode and foreign keys enabled.

    Creates the parent directory if it does not yet exist so the container
    can start cleanly even when the data volume is empty.
    """
    path_str = path or settings.db_path
    logger.debug(f"open_db opening {path_str}")
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    # timeout is SQLite's busy timeout: how long a writer waits for the lock
    # before raising "database is locked". The 5s default is too tight when a
    # whole feed 404s at once and every request wants the writer lock.
    db = await aiosqlite.connect(path_str, timeout=30)
    # Row objects behave like dicts — access columns by name throughout the codebase.
    db.row_factory = aiosqlite.Row
    # WAL allows concurrent readers while the scheduler is writing.
    await db.execute("PRAGMA journal_mode=WAL")
    # Enforce ON DELETE CASCADE on the items → feeds foreign key.
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db(request: Request) -> aiosqlite.Connection:
    """FastAPI dependency: the process-wide connection opened at startup.

    Opening one per request cost a blocking mkdir, an aiosqlite.connect that
    starts an OS thread and two PRAGMA round-trips, on every request including
    the highest-rate route in the app. aiosqlite serialises statements on the
    connection's worker thread, so DB access queues app-wide; with WAL and
    queries this small that is cheaper than a thread per request.

    Sharing one connection means sharing one implicit transaction: any write
    here, single statement or not, has to go through write_transaction rather
    than assume it owns the connection. The scheduler is kept on a separate
    connection for the same reason.

    Work that outlives the request still needs run_with_own_db — a streaming
    body or a warm task running after the route returned must not borrow a
    connection whose lifetime is the request's.
    """
    return request.app.state.db


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]

# The shared connection's write lock. It lived in src/api/items.py as a private
# name while this module's get_db docstring named it from another package, so
# no other writer could take it — and post_setup, the other request-path writer,
# did not. The invariant belongs with the resource it protects.
_write_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def write_transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """Serialise a write on the shared connection, then commit.

    get_db hands every request the same connection, and sqlite3 opens one
    implicit transaction per connection rather than per coroutine. Every write
    needs this, not just a multi-statement one: a bare single-statement
    execute + commit by one coroutine commits whatever another coroutine has
    in flight. Without this two overlapping writers share a transaction and
    either one's ROLLBACK discards the other's statements, leaving seen_media
    written and items.seen_at not (F11) — or discarding a TOTP secret
    mid-setup.

    BaseException rather than Exception: a CancelledError arriving at any await
    inside the block would otherwise unwind past the rollback and leave the
    connection holding a RESERVED lock, with every run_with_own_db write then
    waiting out the 30 s busy timeout and WAL unable to checkpoint.

    The rollback is suppressed because it is I/O on a possibly-broken
    connection; letting it propagate would replace the exception that describes
    what actually went wrong.
    """
    async with _write_lock:
        try:
            yield
            await db.commit()
        except BaseException:
            with contextlib.suppress(BaseException):
                await db.rollback()
            raise


# The connection post-response work writes on. Streaming bodies and warm tasks
# run after the route returned, so they cannot borrow the request connection —
# but opening one per call meant a blocking mkdir, an aiosqlite.connect that
# spawns an OS thread and two PRAGMA round trips for every cached media file.
_writer_db: aiosqlite.Connection | None = None


def set_writer_db(db: aiosqlite.Connection | None) -> None:
    """Install (or clear) the long-lived connection run_with_own_db writes on."""
    global _writer_db
    _writer_db = db


async def run_with_own_db(
    label: str,
    write: Callable[[aiosqlite.Connection], Awaitable[object]],
) -> None:
    """Run one DB write on a fresh connection, logging and swallowing failures.

    For work that outlives the request that started it: fire-and-forget warm
    tasks, and streaming-response bodies, which run after the route function
    has returned and its request-scoped connection has already been closed.
    Borrowing that connection raises "no active connection" instead.
    """
    try:
        if _writer_db is not None:
            await write(_writer_db)
            return
        db = await open_db()
        try:
            await write(db)
        finally:
            await db.close()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{label} failed: {exc}")
