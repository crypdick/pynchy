"""Direct command execution for the message pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import pynchy.types as types
from pynchy.config import get_settings
from pynchy.event_bus import Event, MessageEvent
from pynchy.host.orchestrator.messaging.approval_handler import ApprovalDeps
from pynchy.logger import logger
from pynchy.state import store_message_direct
from pynchy.utils import run_shell_command


class DirectCommandDeps(ApprovalDeps, Protocol):
    async def broadcast_to_channels(
        self, chat_jid: str, event: types.OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...

    def emit(self, event: Event) -> None: ...


async def execute_direct_command(
    deps: DirectCommandDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    message: types.NewMessage,
    command: str,
) -> None:
    """Execute a user command directly without LLM approval."""
    s = get_settings()
    logger.info("Executing direct command", group=group.name, command=command[:100])

    result = await run_shell_command(
        command,
        cwd=str(s.groups_dir / group.folder),
        timeout_seconds=30,
    )

    if result.start_error:
        await deps.broadcast_host_message(chat_jid, f"❌ Command failed: {result.start_error}")
        logger.error("Direct command error", group=group.name, error=result.start_error)
        return

    if result.timed_out:
        await deps.broadcast_host_message(chat_jid, "⏱️ Command timed out (30s limit)")
        logger.warning("Direct command timeout", group=group.name, command=command[:100])
        return

    if result.returncode == 0:
        output, status_emoji = result.stdout or "(no output)", "✅"
    else:
        output, status_emoji = result.stderr or result.stdout or "(no output)", "❌"

    ts = datetime.now(UTC).isoformat()
    output_text = f"{status_emoji} Command output (exit {result.returncode}):\n```\n{output}\n```"

    await store_message_direct(
        message_id=f"command-output-{message.id}",
        chat_jid=chat_jid,
        sender="command_output",
        sender_name="command",
        content=output_text,
        timestamp=ts,
        is_from_me=True,
        message_type="host",
        metadata={
            "source": "direct_command",
            "command": command,
            "exit_code": result.returncode,
            "source_message_id": message.id,
            "workspace_name": group.name,
            "workspace_folder": group.folder,
        },
    )

    event = types.OutboundEvent(
        type=types.OutboundEventType.TOOL_RESULT,
        content=output_text,
        metadata={"verbose": True},
    )
    await deps.broadcast_to_channels(chat_jid, event)

    deps.emit(
        MessageEvent(
            chat_jid=chat_jid,
            sender_name="command",
            content=output_text,
            timestamp=ts,
            is_bot=True,
        )
    )

    logger.info(
        "Direct command executed",
        group=group.name,
        exit_code=result.returncode,
        output_len=len(output),
    )
