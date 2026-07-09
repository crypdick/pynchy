"""Database connection and write utilities.

Single module-level connection, initialized by init_database().
Schema definition and migrations live in :mod:`schema`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import (
    AsyncIterator,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from pynchy.config import get_settings
from pynchy.state.schema import create_schema

_db: aiosqlite.Connection | None = None

# Shared write lock for multi-statement DB transactions — see atomic_write().
#
# pynchy uses a single aiosqlite connection shared across many concurrent
# asyncio coroutines.  Python's sqlite3 manages transactions implicitly:
# the first DML statement auto-opens a transaction
# on the *connection*, not per-coroutine.  Two coroutines whose DML
# interleaves at await points share the same implicit transaction — a
# rollback() from one silently undoes the other's uncommitted work.
#
# Any write path that spans multiple DML statements MUST use atomic_write()
# so no concurrent coroutine can interleave.
_write_lock: asyncio.Lock | None = None

_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UPDATE_TABLES = frozenset({"scheduled_tasks", "host_jobs"})


def _safe_update_identifier(identifier: str, *, allowed: frozenset[str]) -> str:
    """Return an allowlisted SQL identifier, rejecting fragments."""
    if identifier not in allowed or not _SQL_IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier!r}")
    return identifier


@asynccontextmanager
async def atomic_write() -> AsyncIterator[aiosqlite.Connection]:
    """Context manager for multi-statement DB writes.

    Acquires the write lock, yields the connection, and commits on
    success or rolls back on failure.  Every write path that spans
    multiple DML statements (first execute → commit) MUST use this
    so no concurrent coroutine can interleave.
    """
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()

    db = _get_db()
    async with _write_lock:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


def _get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db


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
    table_name = _safe_update_identifier(table, allowed=_UPDATE_TABLES)

    for key, value in updates.items():
        if key in allowed_update_fields:
            field_name = _safe_update_identifier(key, allowed=allowed_update_fields)
            fields.append(f"{field_name} = ?")
            values.append(value)

    if not fields:
        return

    values.append(row_id)
    db = _get_db()
    # S608 audit: table and field names are allowlisted and identifier-validated above.
    await db.execute(
        f"UPDATE {table_name} SET {', '.join(fields)} WHERE id = ?",  # noqa: S608, RUF100
        values,
    )
    await db.commit()


async def init_database() -> None:
    """Initialize the database connection and schema."""
    global _db
    db_path = get_settings().data_dir / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(str(db_path))
    _db.row_factory = aiosqlite.Row
    await create_schema(_db)


async def init_test_database() -> None:
    """Create an in-memory database for tests.

    Uses ``stop()`` + thread join instead of ``await close()`` because
    pytest-asyncio creates a fresh event loop per test function.  A
    lingering connection's worker thread targets the (now-dead) loop it
    was created on via ``call_soon_threadsafe``, so ``await close()`` hangs.
    ``stop()`` bypasses the loop entirely — it puts the close command
    directly on the worker queue and lets the thread exit on its own.
    """
    global _db
    if _db is not None:
        _db.stop()
        if _db._thread is not None and _db._thread.is_alive():
            _db._thread.join(timeout=2)
    _db = await aiosqlite.connect(":memory:")
    _db.row_factory = aiosqlite.Row
    await create_schema(_db)
