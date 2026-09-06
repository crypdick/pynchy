"""Chat-parent integrity shared by message and outbound writers."""

from __future__ import annotations

from aiosqlite import Connection


async def ensure_chat_parent(
    database: Connection,
    chat_jid: str,
    timestamp: str,
) -> None:
    """Create the parent row required by chat-owned durable records."""
    await database.execute(
        """
        INSERT OR IGNORE INTO chats (jid, name, last_message_time)
        VALUES (?, ?, ?)
        """,
        (chat_jid, chat_jid, timestamp),
    )
