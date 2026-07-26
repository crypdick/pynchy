"""Schema migrations owned by durable in-flight turns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiosqlite import OperationalError

if TYPE_CHECKING:
    import aiosqlite

from pynchy.logger import logger

_LEGACY_RUNTIME_COLUMNS = (
    "scheduled_base_chat_jid",
    "scheduled_thread_slot",
)


async def drop_legacy_scheduled_runtime_metadata(
    database: aiosqlite.Connection,
) -> None:
    """Drop allocator metadata superseded by the checkpoint's runtime binding."""
    cursor = await database.execute("PRAGMA table_info(in_flight_turns)")
    columns = {row[1] for row in await cursor.fetchall()}
    for column in _LEGACY_RUNTIME_COLUMNS:
        if column not in columns:
            continue
        try:
            await database.execute(f"ALTER TABLE in_flight_turns DROP COLUMN {column}")
            logger.info("Dropped legacy in-flight runtime column", column=column)
        except OperationalError as exc:
            logger.warning(
                "Failed to drop legacy in-flight runtime column",
                column=column,
                err=str(exc),
            )
    await database.commit()
