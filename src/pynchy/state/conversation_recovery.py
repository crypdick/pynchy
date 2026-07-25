"""Startup repair for durable routed-conversation execution state."""

from __future__ import annotations

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves recovery annotations at runtime.
    Connection,
)

from pynchy.config.workspace_names import dynamic_thread_folder
from pynchy.conversation.models import ConversationId
from pynchy.conversation.workspaces import conversation_id_from_folder
from pynchy.state.connection import atomic_write


async def prepare_conversation_delivery_recovery() -> int:
    """Repair reset-hidden work, then release retryable orphaned claims.

    Reset ordering can commit a control-thread clear while retaining the
    conversation-owned session or the FIFO claim synchronously injected by the
    preceding turn's completion callback. The clear boundary distinguishes
    discarded work from post-reset deliveries.
    """
    async with atomic_write() as database:
        repaired_sessions = await _repair_scheduled_session_bindings(database)
        await database.execute(
            """
            UPDATE routed_conversations
            SET session_id = NULL
            WHERE session_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM conversation_control_bindings AS binding
                  JOIN chats ON chats.jid = binding.thread_jid
                  WHERE binding.conversation_id = routed_conversations.id
                    AND chats.cleared_at IS NOT NULL
                    AND julianday(routed_conversations.updated_at)
                        <= julianday(chats.cleared_at)
              )
            """
        )
        retired = await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', completed_at = (
                SELECT chats.cleared_at
                FROM conversation_control_bindings AS binding
                JOIN chats ON chats.jid = binding.thread_jid
                WHERE binding.conversation_id = conversation_deliveries.conversation_id
            )
            WHERE EXISTS (
                SELECT 1
                FROM conversation_control_bindings AS binding
                JOIN chats ON chats.jid = binding.thread_jid
                WHERE binding.conversation_id = conversation_deliveries.conversation_id
                  AND chats.cleared_at IS NOT NULL
                  AND julianday(conversation_deliveries.received_at)
                      <= julianday(chats.cleared_at)
            )
              AND (
                  status = 'pending'
                  OR (
                      status = 'claimed'
                      AND NOT EXISTS (
                          SELECT 1 FROM in_flight_turns
                          WHERE in_flight_turns.conversation_claim_id =
                              conversation_deliveries.claim_id
                      )
                  )
              )
            """
        )
        released = await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'pending', claim_id = NULL, claimed_at = NULL
            WHERE status = 'claimed'
              AND NOT EXISTS (
                  SELECT 1 FROM in_flight_turns
                  WHERE in_flight_turns.conversation_claim_id = conversation_deliveries.claim_id
              )
            """
        )
        return repaired_sessions + retired.rowcount + released.rowcount


async def _repair_scheduled_session_bindings(database: Connection) -> int:
    """Forget routed sessions copied from a scheduled run sharing its control thread.

    Scheduled and routed work use different runtime folders even when they post
    to the same provider thread. A matching session under both identities can
    only occur when session binding crosses invocation ownership and uses the
    shared thread JID instead.
    """
    cursor = await database.execute(
        """
        SELECT conversation.id, conversation.workspace, conversation.session_id,
               binding.thread_jid
        FROM routed_conversations AS conversation
        JOIN conversation_control_bindings AS binding
          ON binding.conversation_id = conversation.id
        WHERE conversation.session_id IS NOT NULL
        """
    )
    repaired = 0
    for row in await cursor.fetchall():
        conversation_id = ConversationId(row["id"])
        scheduled_folder = dynamic_thread_folder(row["workspace"], row["thread_jid"])
        sessions_cursor = await database.execute(
            "SELECT group_folder FROM sessions WHERE session_id = ?",
            (row["session_id"],),
        )
        session_folders = [session["group_folder"] for session in await sessions_cursor.fetchall()]
        if scheduled_folder not in session_folders:
            continue

        await database.execute(
            "UPDATE routed_conversations SET session_id = NULL WHERE id = ?",
            (conversation_id,),
        )
        for folder in session_folders:
            if folder == scheduled_folder or conversation_id_from_folder(folder) == conversation_id:
                await database.execute(
                    "DELETE FROM sessions WHERE group_folder = ?",
                    (folder,),
                )
        repaired += 1
    return repaired
