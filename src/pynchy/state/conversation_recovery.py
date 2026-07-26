"""Startup repair for durable routed-conversation execution state."""

from __future__ import annotations

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves recovery annotations at runtime.
    Connection,
)

from pynchy.config.workspace_names import dynamic_thread_folder, parent_workspace_name
from pynchy.conversation.models import ConversationId
from pynchy.conversation.workspaces import (
    conversation_id_from_folder,
    routed_conversation_folder,
)
from pynchy.logger import logger
from pynchy.state.connection import atomic_write


async def prepare_conversation_runtime_ownership_recovery() -> int:
    """Move thread-derived runtimes to conversation ownership before state load."""
    async with atomic_write() as database:
        return await _migrate_legacy_conversation_runtime_ownership(database)


async def prepare_conversation_delivery_recovery() -> int:
    """Repair reset-hidden work, then release retryable orphaned claims.

    Reset ordering can commit a control-thread clear while retaining the
    conversation-owned session or the FIFO claim synchronously injected by the
    preceding turn's completion callback. The clear boundary distinguishes
    discarded work from post-reset deliveries.
    """
    async with atomic_write() as database:
        migrated_runtime_state = await _migrate_legacy_conversation_runtime_ownership(database)
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
                  status IN ('held', 'pending')
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
        await database.execute(
            """
            DELETE FROM webhook_effect_candidates
            WHERE EXISTS (
                SELECT 1 FROM conversation_deliveries
                WHERE conversation_deliveries.provider =
                          webhook_effect_candidates.provider
                  AND conversation_deliveries.route =
                          webhook_effect_candidates.route
                  AND conversation_deliveries.delivery_id =
                          webhook_effect_candidates.delivery_id
                  AND conversation_deliveries.status = 'completed'
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
        return migrated_runtime_state + retired.rowcount + released.rowcount


async def _migrate_legacy_conversation_runtime_ownership(database: Connection) -> int:
    """Collapse thread-derived runtimes into their routed conversation owner."""
    cursor = await database.execute(
        """
        SELECT conversation.id, conversation.workspace, conversation.session_id,
               binding.parent_workspace, binding.thread_jid
        FROM routed_conversations AS conversation
        JOIN conversation_control_bindings AS binding
          ON binding.conversation_id = conversation.id
        """
    )
    migrated = 0
    for row in await cursor.fetchall():
        conversation_id = ConversationId(row["id"])
        profile_cursor = await database.execute(
            "SELECT folder FROM registered_groups WHERE jid = ?",
            (row["thread_jid"],),
        )
        profile = await profile_cursor.fetchone()
        profile_folder = str(profile["folder"]) if profile is not None else None
        workspace = str(row["workspace"])
        profile_workspace = (
            parent_workspace_name(profile_folder)
            if profile_folder is not None
            and conversation_id_from_folder(profile_folder) == conversation_id
            else None
        )
        if (
            profile_workspace is not None
            and profile_workspace != workspace
            and workspace == row["parent_workspace"]
        ):
            repaired_owner = await database.execute(
                "UPDATE routed_conversations SET workspace = ? WHERE id = ? AND workspace = ?",
                (profile_workspace, conversation_id, workspace),
            )
            migrated += repaired_owner.rowcount
            workspace = profile_workspace

        legacy_folder = dynamic_thread_folder(workspace, row["thread_jid"])
        routed_folder = routed_conversation_folder(workspace, conversation_id)
        target_cursor = await database.execute(
            "SELECT jid FROM registered_groups WHERE folder = ?",
            (routed_folder,),
        )
        target = await target_cursor.fetchone()
        if target is not None and target["jid"] != row["thread_jid"]:
            logger.warning(
                "Legacy routed workspace ownership migration blocked",
                conversation_id=conversation_id,
                legacy_folder=legacy_folder,
                routed_folder=routed_folder,
                target_jid=target["jid"],
            )
            continue
        # A hierarchy migration can change only the owner prefix while retaining
        # the immutable conversation identity and control JID. Recognize that
        # shape without ever moving a folder owned by another conversation.
        profile_source = (
            profile_folder
            if profile_folder is not None
            and (
                profile_folder == legacy_folder
                or conversation_id_from_folder(profile_folder) == conversation_id
            )
            else legacy_folder
        )
        if profile_source != routed_folder:
            moved_workspace = await database.execute(
                """
                UPDATE registered_groups
                SET folder = ?
                WHERE jid = ? AND folder = ?
                """,
                (routed_folder, row["thread_jid"], profile_source),
            )
            migrated += moved_workspace.rowcount

        if row["session_id"] is None:
            continue
        sessions_cursor = await database.execute(
            "SELECT group_folder FROM sessions WHERE session_id = ?",
            (row["session_id"],),
        )
        session_folders = [session["group_folder"] for session in await sessions_cursor.fetchall()]
        source_folders = [legacy_folder]
        if profile_source not in source_folders and profile_source != routed_folder:
            source_folders.append(profile_source)
        session_source = next(
            (folder for folder in source_folders if folder in session_folders),
            None,
        )
        if session_source is None:
            continue

        await database.execute(
            """
            INSERT INTO sessions (group_folder, session_id)
            VALUES (?, ?)
            ON CONFLICT(group_folder) DO UPDATE SET
                session_id = excluded.session_id
            """,
            (
                routed_folder,
                row["session_id"],
            ),
        )
        await database.execute(
            """
            INSERT INTO session_security_taint (
                group_folder, corruption_tainted, secret_tainted, updated_at
            )
            SELECT ?, corruption_tainted, secret_tainted, updated_at
            FROM session_security_taint
            WHERE group_folder = ?
            ON CONFLICT(group_folder) DO UPDATE SET
                corruption_tainted = MAX(
                    session_security_taint.corruption_tainted,
                    excluded.corruption_tainted
                ),
                secret_tainted = MAX(
                    session_security_taint.secret_tainted,
                    excluded.secret_tainted
                ),
                updated_at = MAX(
                    session_security_taint.updated_at,
                    excluded.updated_at
                )
            """,
            (routed_folder, session_source),
        )
        await database.execute(
            "DELETE FROM sessions WHERE group_folder = ?",
            (session_source,),
        )
        await database.execute(
            "DELETE FROM session_security_taint WHERE group_folder = ?",
            (session_source,),
        )
        migrated += 1
    return migrated
