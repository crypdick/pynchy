"""Chat-parent integrity shared by message and outbound writers."""

from __future__ import annotations

from aiosqlite import Connection  # noqa: TC002, RUF100 - beartype resolves annotations.


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


async def backfill_missing_chat_parents(database: Connection) -> int:
    """Restore missing parents without deleting historical child records."""
    cursor = await database.execute(
        """
        INSERT OR IGNORE INTO chats (jid, name, last_message_time)
        SELECT activity.chat_jid,
               COALESCE(profile.name, activity.chat_jid),
               activity.last_message_time
        FROM (
            SELECT chat_jid, MAX(timestamp) AS last_message_time
            FROM (
                SELECT chat_jid, timestamp FROM messages
                UNION ALL
                SELECT chat_jid, timestamp FROM outbound_ledger
            )
            GROUP BY chat_jid
        ) AS activity
        LEFT JOIN registered_groups AS profile
          ON profile.jid = activity.chat_jid
        """
    )
    return cursor.rowcount
