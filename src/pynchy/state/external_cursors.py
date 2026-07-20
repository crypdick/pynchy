"""Durable cursors for provider connection runtimes."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.state.connection import _get_db, atomic_write


async def get_external_provider_cursor(provider: str, connection_name: str) -> str | None:
    """Return the last provider cursor committed by one named connection."""
    cursor = await _get_db().execute(
        """
        SELECT cursor_value FROM external_provider_cursors
        WHERE provider = ? AND connection_name = ?
        """,
        (provider, connection_name),
    )
    row = await cursor.fetchone()
    return row["cursor_value"] if row is not None else None


async def set_external_provider_cursor(
    provider: str,
    connection_name: str,
    cursor_value: str,
) -> None:
    """Commit one non-empty provider cursor after processing its batch."""
    if not cursor_value.strip():
        raise ValueError("External provider cursor must not be empty")
    async with atomic_write() as database:
        await database.execute(
            """
            INSERT INTO external_provider_cursors (
                provider, connection_name, cursor_value, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(provider, connection_name) DO UPDATE SET
                cursor_value = excluded.cursor_value,
                updated_at = excluded.updated_at
            """,
            (provider, connection_name, cursor_value, datetime.now(UTC).isoformat()),
        )
