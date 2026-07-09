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
from dataclasses import dataclass
from typing import Any

import aiosqlite

from pynchy.config import get_settings
from pynchy.state.schema import create_schema


@dataclass(slots=True)
class _ConnectionState:
    db: aiosqlite.Connection | None = None
    write_lock: asyncio.Lock | None = None


_state = _ConnectionState()

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
    if _state.write_lock is None:
        _state.write_lock = asyncio.Lock()

    db = _get_db()
    async with _state.write_lock:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


def _get_db() -> aiosqlite.Connection:
    if _state.db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _state.db


async def _stop_connection(db: aiosqlite.Connection) -> None:
    """Stop the worker thread using the public stop future.

    ``aiosqlite.Connection.stop()`` returns a future that resolves once the
    background worker has processed the shutdown sentinel. Waiting on that
    future avoids reaching into the private ``_thread`` attribute while keeping
    the existing bounded shutdown behavior.
    """
    future = db.stop()
    if future is None:
        return

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
    db_path = get_settings().data_dir / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
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
    db.row_factory = aiosqlite.Row
    _state.db = db
    await create_schema(db)
