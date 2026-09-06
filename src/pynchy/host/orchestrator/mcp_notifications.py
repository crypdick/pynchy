"""Channel-visible notifications for MCP lifecycle failures."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)

from pynchy.agent_protocol.api import (
    McpStartupFailure,
)
from pynchy.logger import logger


async def notify_mcp_startup_failures(
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    chat_jid: str,
    failures: tuple[McpStartupFailure, ...],
) -> None:
    """Publish newly observed optional-tool failures without blocking the agent."""
    details = ", ".join(f"{failure.server_name} ({failure.reason})" for failure in failures)
    message = (
        f"⚠️ MCP tool unavailable: {details}. Continuing without it; Pynchy will retry in 5 minutes."
    )
    try:
        # Log-only failures leave the channel user unaware that a requested tool is absent.
        await broadcast_host_message(chat_jid, message)
    except Exception:  # noqa: BLE001 - allow: exception-handling; best-effort notification.
        logger.warning("Failed to send MCP startup notice", chat_jid=chat_jid, exc_info=True)
