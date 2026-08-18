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

    Creates the parent directory first, then sets a 30s busy timeout, Row as
    the row factory, WAL, and foreign_keys=ON — none of which is a default.
    Rationale for each in spec.md §12.1.
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

    Requests share one connection rather than one each, and pay for it with a
    shared implicit transaction: every write on it must go through
    write_transaction. Why, in spec.md §12.1.
    """
    return request.app.state.db


DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]

# Lives here, beside the connection it guards, so every writer takes this one.
_write_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def write_transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """Serialise a write on the shared connection, then commit.

    Every write needs this, not only a multi-statement one, because the shared
    connection's transaction is implicit and per-connection, not per coroutine.
    Catches BaseException (not just Exception) so a CancelledError still rolls
    back instead of leaving the connection holding the lock. The rollback's own
    exception is suppressed — it is I/O on a possibly-broken connection, and
    letting it propagate would replace the exception that describes what
    actually went wrong. Full rationale in spec.md §12.1.
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
            await db.commit()
        finally:
            await db.close()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"{label} failed: {exc}")
