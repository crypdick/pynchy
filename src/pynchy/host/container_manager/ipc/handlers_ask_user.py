"""IPC handler for ask_user: prefix.

When the container-side ask_user MCP tool writes a task file with
type "ask_user:ask", this handler:
- Resolves which chat JID and channel own the source group
- Stores a pending question via the pending_questions state manager
- Forwards the question to the channel's send_ask_user method
- Writes an IPC error response if the channel doesn't support ask_user
"""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.ipc.registry import register_prefix
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.orchestrator.messaging.pending_questions import (
    create_pending_question,
    resolve_pending_question,
    update_message_id,
)
from pynchy.logger import logger


async def _handle_ask_user_request(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    """Handle an ask_user:ask IPC task from a container."""
    request_id = data.get("request_id")
    if not request_id:
        logger.warning("ask_user task missing request_id", source_group=source_group)
        return

    questions = data.get("questions", [])

    # Resolve chat_jid for this group
    chat_jid = resolve_chat_jid(source_group, deps)

    if chat_jid is None:
        logger.warning(
            "No workspace found for source group",
            source_group=source_group,
        )
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": f"No workspace found for group '{source_group}'"},
        )
        return

    # Resolve which channel owns this JID
    channels = deps.channels()
    channel = next((ch for ch in channels if ch.owns_jid(chat_jid)), None)

    if channel is None:
        logger.warning(
            "No channel owns JID for ask_user",
            chat_jid=chat_jid,
            source_group=source_group,
        )
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": f"No channel found for JID '{chat_jid}'"},
        )
        return

    # Resolve session_id
    active_sessions = deps.get_active_sessions()
    session_id = active_sessions.get(chat_jid, "")

    # Store the pending question
    create_pending_question(
        request_id=request_id,
        source_group=source_group,
        chat_jid=chat_jid,
        channel_name=channel.name,
        session_id=session_id,
        questions=questions,
    )

    # Send to channel if it supports ask_user
    if hasattr(channel, "send_ask_user"):
        message_id = await channel.send_ask_user(chat_jid, request_id, questions)
        if message_id:
            update_message_id(request_id, source_group, message_id)
    else:
        logger.warning(
            "Channel does not support send_ask_user",
            channel=channel.name,
            source_group=source_group,
        )
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": f"Channel '{channel.name}' does not support interactive questions"},
        )
        # Clean up the pending question file — no one will answer it.
        resolve_pending_question(request_id, source_group)


register_prefix("ask_user:", _handle_ask_user_request)
