"""Direct command execution for the message pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.event_bus import MessageEvent
from pynchy.host.orchestrator.host_shell import run_shell_command
from pynchy.host.orchestrator.messaging.deps import DirectCommandDeps, DirectCommandOutput
from pynchy.logger import logger
from pynchy.plugins.api import NewMessage, OutboundEvent, OutboundEventType
from pynchy.workspace.api import (
    WorkspaceProfile,
)


async def execute_direct_command(
    deps: DirectCommandDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
    command: str,
) -> None:
    """Execute a user command directly without LLM approval."""
    logger.info("Executing direct command", group=group.name, command=command[:100])

    result = await run_shell_command(
        command,
        cwd=str(deps.direct_command_workdir(group)),
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

    await deps.record_direct_command_output(
        DirectCommandOutput(
            chat_jid=chat_jid,
            group=group,
            source_message=message,
            command=command,
            exit_code=result.returncode,
            content=output_text,
            timestamp=ts,
        )
    )

    event = OutboundEvent(
        type=OutboundEventType.TOOL_RESULT,
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
