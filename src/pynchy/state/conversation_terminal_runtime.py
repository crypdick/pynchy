"""Discover durable runtime projections for terminal conversation retirement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pynchy.conversation.api import (
    ConversationId,  # noqa: TC001, RUF100 - beartype resolves annotations.
    TerminalConversationRetirement,
    conversation_id_from_folder,
    dynamic_thread_folder,
    routed_conversation_folder,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
)
from pynchy.state.connection import _get_db

if TYPE_CHECKING:
    from aiosqlite import Connection, Row
else:
    Connection = Any
    Row = Any


async def terminal_runtime_resources(
    database: Connection,
    conversation_id: ConversationId,
    workspace: str,
    binding: Row | None,
) -> tuple[set[GroupFolder], set[ChatJid]]:
    """Return every durable runtime folder and registered thread for one conversation."""
    folders = {GroupFolder(routed_conversation_folder(workspace, conversation_id))}
    if binding is not None:
        folders.add(GroupFolder(dynamic_thread_folder(workspace, binding["thread_jid"])))
        folders.add(
            GroupFolder(dynamic_thread_folder(binding["parent_workspace"], binding["thread_jid"]))
        )

    receipt_cursor = await database.execute(
        """
        SELECT DISTINCT receipt.workspace
        FROM conversation_deliveries AS delivery
        JOIN webhook_receipts AS receipt
          ON receipt.provider = delivery.provider
         AND receipt.route = delivery.route
         AND receipt.delivery_id = delivery.delivery_id
        WHERE delivery.conversation_id = ?
        """,
        (conversation_id,),
    )
    for row in await receipt_cursor.fetchall():
        folders.add(GroupFolder(routed_conversation_folder(row["workspace"], conversation_id)))

    task_cursor = await database.execute(
        """
        SELECT bound_group_folder
        FROM scheduled_tasks
        WHERE conversation_id = ? AND bound_group_folder IS NOT NULL
        """,
        (conversation_id,),
    )
    for row in await task_cursor.fetchall():
        folder = GroupFolder(row["bound_group_folder"])
        if conversation_id_from_folder(folder) == conversation_id:
            folders.add(folder)

    projection_cursor = await database.execute(
        """
        SELECT group_folder AS folder FROM in_flight_turns
        UNION
        SELECT group_folder AS folder FROM sessions
        UNION
        SELECT group_folder AS folder FROM session_security_taint
        UNION
        SELECT folder FROM registered_groups
        """
    )
    for row in await projection_cursor.fetchall():
        folder = GroupFolder(row["folder"])
        if conversation_id_from_folder(folder) == conversation_id:
            folders.add(folder)

    workspace_jids = {ChatJid(binding["thread_jid"])} if binding is not None else set()
    profile_cursor = await database.execute("SELECT jid, folder FROM registered_groups")
    for row in await profile_cursor.fetchall():
        if GroupFolder(row["folder"]) in folders:
            workspace_jids.add(ChatJid(row["jid"]))
    return folders, workspace_jids


async def get_terminal_conversation_retirement(
    conversation_id: ConversationId,
) -> TerminalConversationRetirement | None:
    """Return durable local cleanup targets only while terminal intent remains current."""
    database = _get_db()
    conversation_cursor = await database.execute(
        """
        SELECT workspace, control_closed, control_state_revision
        FROM routed_conversations
        WHERE id = ?
        """,
        (conversation_id,),
    )
    conversation = await conversation_cursor.fetchone()
    if conversation is None or not bool(conversation["control_closed"]):
        return None
    binding_cursor = await database.execute(
        """
        SELECT parent_workspace, thread_jid
        FROM conversation_control_bindings
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    )
    binding = await binding_cursor.fetchone()
    folders, workspace_jids = await terminal_runtime_resources(
        database,
        conversation_id,
        conversation["workspace"],
        binding,
    )
    return TerminalConversationRetirement(
        runtime_folders=tuple(sorted(folders)),
        runtime_workspace_jids=tuple(sorted(workspace_jids)),
        control_state_revision=conversation["control_state_revision"],
    )
