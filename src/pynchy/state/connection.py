"""Database connection and write utilities.

Single module-level connection, initialized by init_database().
Schema definition lives in :mod:`schema`.
"""

from __future__ import annotations

import asyncio
from collections.abc import (
    AsyncIterator,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiosqlite

from pynchy.state.schema import create_schema


@dataclass(frozen=True, slots=True)
class StateRuntimeConfig:
    """Resolved host values required to initialize state."""

    database_path: Path


@dataclass(slots=True)
class _ConnectionState:
    db: aiosqlite.Connection | None = None
    write_lock: asyncio.Lock | None = None


_state = _ConnectionState()

_DATABASE_NOT_INITIALIZED_MESSAGE = "Database not initialized. Call init_database() first."
_FOREIGN_KEYS_NOT_ENABLED_MESSAGE = "SQLite foreign-key enforcement could not be enabled"


@asynccontextmanager
async def atomic_write() -> AsyncIterator[aiosqlite.Connection]:
    """Context manager for multi-statement DB writes.

    Acquires the write lock, yields the connection, and commits on
    success or rolls back on failure. Every runtime write path, including
    a single DML statement, MUST use this lock: all coroutines share one
    connection, so an interleaved commit or rollback would settle every
    pending write on that connection.
    """
    if _state.write_lock is None:
        _state.write_lock = asyncio.Lock()

    db = _get_db()
    async with _state.write_lock:
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


def _get_db() -> aiosqlite.Connection:
    if _state.db is None:
        raise RuntimeError(_DATABASE_NOT_INITIALIZED_MESSAGE)
    return _state.db


async def _stop_connection(db: aiosqlite.Connection) -> None:
    """Stop the worker thread using the public stop future.

    ``aiosqlite.Connection.stop()`` returns a future that resolves once the
    background worker has processed the shutdown sentinel. Waiting on that
    future avoids reaching into the private ``_thread`` attribute while keeping
    the existing bounded shutdown behavior.
    """
    future = cast("asyncio.Future[Any]", db.stop())
    done, _pending = await asyncio.wait({future}, timeout=2)
    if future in done:
        await future


async def _update_by_id(
    table: str,
    row_id: str,
    updates: dict[str, Any],
    allowed_fields: set[str],
) -> None:
    """Build and execute a dynamic UPDATE for an allowlisted set of fields.

    Shared by tasks, host_jobs, and any future table that needs
    partial-update-by-primary-key semantics.  Silently skips keys
    not in *allowed_fields* so callers don't need to pre-filter.
    """
    fields: list[str] = []
    values: list[Any] = []

    allowed_update_fields = frozenset(allowed_fields)
    table_name = table

    for key, value in updates.items():
        if key in allowed_update_fields:
            fields.append(f"{key} = ?")
            values.append(value)

    if not fields:
        return

    values.append(row_id)
    async with atomic_write() as db:
        # S608 audit: table and field names are fixed by internal allowlists.
        await db.execute(
            f"UPDATE {table_name} SET {', '.join(fields)} WHERE id = ?",  # noqa: S608
            values,
        )


async def _enable_foreign_keys(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA foreign_keys = ON")
    cursor = await db.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    if row is None or row[0] != 1:
        raise RuntimeError(_FOREIGN_KEYS_NOT_ENABLED_MESSAGE)


async def init_database(config: StateRuntimeConfig) -> None:
    """Initialize the database connection and schema."""
    db_path = config.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row  # noqa: V101
    await _enable_foreign_keys(db)
    _state.db = db
    await create_schema(db)


async def init_test_database() -> None:
    """Create an in-memory database for tests.

    Uses ``stop()`` + thread join instead of ``await close()`` because
    pytest-asyncio creates a fresh event loop per test function.  A
    lingering connection's worker thread targets the (now-dead) loop it
    was created on via ``call_soon_threadsafe``, so ``await close()`` hangs.
    ``stop()`` bypasses the loop entirely — it puts the close command
    directly on the worker queue and lets the thread exit on its own.
    """
    if _state.db is not None:
        await _stop_connection(_state.db)
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row  # noqa: V101
    await _enable_foreign_keys(db)
    _state.db = db
    await create_schema(db)


def close_test_database() -> None:
    """Synchronously stop the in-memory test connection after its loop closes.

    Pytest creates the connection on a function-scoped event loop, so session
    teardown cannot safely await its normal close operation. ``stop()`` sends
    the shutdown sentinel directly to aiosqlite's worker, and the bounded join
    prevents the worker thread leaking into the next test process.
    """
    db = _state.db
    if db is None:
        return
    db.stop()
    thread = db._thread  # noqa: SLF001 - aiosqlite exposes no synchronous join API.
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    _state.db = None
    _state.write_lock = None
