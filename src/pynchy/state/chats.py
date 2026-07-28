"""Chat metadata operations."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.conversation.api import (
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.state.connection import _get_db, atomic_write


async def set_chat_cleared_at(
    chat_jid: str,
    timestamp: str,
) -> tuple[ConversationDeliveryCompletion, ...]:
    """Commit a chat clear boundary and retire routed work hidden behind it.

    A routed control thread owns session state and a provider FIFO in addition
    to ordinary chat history. Those records must cross the clear boundary in
    the same transaction; otherwise a delivery claimed between turn completion
    and the reset can become invisible to polling while still blocking its
    pending siblings.
    """
    async with atomic_write() as database:
        await database.execute(
            "UPDATE chats SET cleared_at = ? WHERE jid = ?",
            (timestamp, chat_jid),
        )
        binding_cursor = await database.execute(
            """
            SELECT conversation_id FROM conversation_control_bindings
            WHERE thread_jid = ?
            """,
            (chat_jid,),
        )
        binding = await binding_cursor.fetchone()
        if binding is None:
            return ()

        conversation_id = ConversationId(binding["conversation_id"])
        await database.execute(
            """
            UPDATE routed_conversations
            SET session_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, conversation_id),
        )
        candidates_cursor = await database.execute(
            """
            SELECT provider, route, delivery_id
            FROM conversation_deliveries
            WHERE conversation_id = ?
              AND julianday(received_at) <= julianday(?)
              AND (
                  status IN ('held', 'pending')
                  OR (
                      status = 'claimed'
                      AND NOT EXISTS (
                          SELECT 1 FROM in_flight_turns
                          WHERE in_flight_turns.conversation_claim_id =
                              conversation_deliveries.claim_id
                            AND in_flight_turns.control_state != 'reset_requested'
                      )
                  )
              )
            ORDER BY sequence
            """,
            (conversation_id, timestamp),
        )
        candidates = await candidates_cursor.fetchall()
        if not candidates:
            return ()
        await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', completed_at = ?
            WHERE conversation_id = ?
              AND julianday(received_at) <= julianday(?)
              AND (
                  status IN ('held', 'pending')
                  OR (
                      status = 'claimed'
                      AND NOT EXISTS (
                          SELECT 1 FROM in_flight_turns
                          WHERE in_flight_turns.conversation_claim_id =
                              conversation_deliveries.claim_id
                            AND in_flight_turns.control_state != 'reset_requested'
                      )
                  )
              )
            """,
            (timestamp, conversation_id, timestamp),
        )
        await database.executemany(
            """
            DELETE FROM webhook_effect_candidates
            WHERE provider = ? AND route = ? AND delivery_id = ?
            """,
            [(row["provider"], row["route"], row["delivery_id"]) for row in candidates],
        )

        completions: list[ConversationDeliveryCompletion] = []
        seen_routes: set[tuple[str, str]] = set()
        for row in candidates:
            route_key = (row["provider"], row["route"])
            if route_key in seen_routes:
                continue
            seen_routes.add(route_key)
            completions.append(
                ConversationDeliveryCompletion(
                    identity=ExternalDeliveryIdentity(
                        provider=ExternalProvider(row["provider"]),
                        route=ExternalRoute(row["route"]),
                        delivery_id=ExternalDeliveryId(row["delivery_id"]),
                    ),
                    conversation_id=conversation_id,
                )
            )
        return tuple(completions)


async def get_chat_cleared_at(chat_jid: str) -> str | None:
    """Return the cleared_at timestamp for a chat, or None if never cleared."""
    db = _get_db()
    cursor = await db.execute("SELECT cleared_at FROM chats WHERE jid = ?", (chat_jid,))
    row = await cursor.fetchone()
    return row["cleared_at"] if row and row["cleared_at"] else None


async def store_chat_metadata(chat_jid: str, timestamp: str, name: str | None = None) -> None:
    """Store chat metadata only (no message content)."""
    db = _get_db()
    if name:
        await db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, name, timestamp),
        )
    else:
        await db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, chat_jid, timestamp),
        )
    await db.commit()


async def update_chat_name(chat_jid: str, name: str) -> None:
    """Update chat name without changing timestamp for existing chats."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET name = excluded.name
        """,
        (chat_jid, name, now),
    )
    await db.commit()


async def get_all_chats() -> list[dict[str, str]]:
    """Get all known chats, ordered by most recent activity."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_chat_jids_by_name(name: str) -> list[str]:
    """Return chat JIDs matching the given human-friendly name (case-insensitive)."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT jid FROM chats WHERE lower(name) = lower(?)",
        (name,),
    )
    rows = await cursor.fetchall()
    return [row["jid"] for row in rows]


async def get_last_group_sync() -> str | None:
    """Get timestamp of last group metadata sync."""
    db = _get_db()
    cursor = await db.execute("SELECT last_message_time FROM chats WHERE jid = '__group_sync__'")
    row = await cursor.fetchone()
    return row["last_message_time"] if row else None


async def set_last_group_sync() -> None:
    """Record that group metadata was synced."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO chats (jid, name, last_message_time) "
        "VALUES ('__group_sync__', '__group_sync__', ?)",
        (now,),
    )
    await db.commit()
