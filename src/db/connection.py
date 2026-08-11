"""Database connections and the write lock that guards the shared one.

open_db() opens a connection held for the process lifetime; src/main.py opens
two, one the requests share via get_db() and one the scheduler keeps to itself.
Every write on the shared connection goes through write_transaction(); writes
with no connection to borrow use run_with_own_db().

DbDep is the public form of get_db(): route handlers in src/api and src/auth
annotate their db parameter with it rather than depending on get_db directly.
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
    """Open an aiosqlite connection configured the way every caller expects.

    The parent directory is created first, so the container starts cleanly on
    an empty data volume.

    None of the four settings is a default:
    - a 30 s busy timeout — how long a writer waits for the lock before raising
      "database is locked". The 5 s default is too tight when many
      run_with_own_db writers contend for it at once.
    - Row as the row factory, so rows index by column name as callers expect.
    - WAL, so readers keep working while the scheduler writes.
    - foreign_keys=ON, which SQLite leaves off; without it the items → feeds
      ON DELETE CASCADE declared in schema.py does nothing.
    """
    path_str = path or settings.db_path
    logger.debug(f"open_db opening {path_str}")
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path_str, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def get_db(request: Request) -> aiosqlite.Connection:
    """FastAPI dependency: the process-wide connection opened at startup.

    aiosqlite serialises statements on the connection's own worker thread, so
    every request queues behind every other; with WAL and queries this small
    that is cheaper than starting a thread and running two PRAGMAs per request.

    The cost is a shared implicit transaction — sqlite3 opens one per
    connection, not per coroutine. Any write on this connection, single
    statement or not, has to go through write_transaction. The scheduler is
    kept on its own connection for the same reason.
    """
    return request.app.state.db


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]

# Lives here, beside the connection it guards, so every writer takes this one.
_write_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def write_transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """Serialise a write on the shared connection, then commit.

    Every write needs this, not only a multi-statement one: two coroutines on
    one connection share one transaction, so a bare execute + commit by one of
    them commits whatever the other has in flight, and either one's rollback
    discards the other's statements.

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


# Known cost: one open and close per call — a blocking mkdir, an
# aiosqlite.connect that spawns an OS thread, two PRAGMA round trips — for every
# cached media file. Moving these writers onto a shared long-lived connection
# needs three things at once: a write lock, an explicit rollback (a private
# connection rolls back for free when it closes mid-write, a shared one does
# not), and a teardown that waits for writes still in flight.
async def run_with_own_db(
    label: str,
    write: Callable[[aiosqlite.Connection], Awaitable[object]],
) -> None:
    """Run one DB write on a fresh connection, logging and swallowing failures.

    For writers with no connection to borrow: background prefetch tasks, and
    streaming-response bodies, which run after the route function has returned.
    A private connection also keeps these writes out of the shared connection's
    implicit transaction — they commit without going through write_transaction.

    Failures are logged and dropped: every caller is fire-and-forget bookkeeping
    (cache digests, dead-URL marks) that must not fail the media it belongs to.
    """
    try:
        db = await open_db()
        try:
            await write(db)
        finally:
            await db.close()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{label} failed: {exc}")
