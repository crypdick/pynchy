"""Message formatting and outbound routing.

Port of src/router.ts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pynchy.config import ASSISTANT_NAME

if TYPE_CHECKING:
    from pynchy.types import Channel, NewMessage

_INTERNAL_TAG_RE = re.compile(r"<internal>[\s\S]*?</internal>")
_HOST_TAG_RE = re.compile(r"^\s*<host>([\s\S]*?)</host>\s*$")


def escape_xml(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def format_messages(messages: list[NewMessage]) -> str:
    """Format messages as XML for the agent prompt.

    NOTE: This is the legacy XML format. Use format_messages_for_sdk() for new code.
    """
    lines = [
        f'<message sender="{escape_xml(m.sender_name)}" time="{m.timestamp}">'
        f"{escape_xml(m.content)}</message>"
        for m in messages
    ]
    return f"<messages>\n{chr(10).join(lines)}\n</messages>"


def format_messages_for_sdk(messages: list[NewMessage]) -> list[dict]:
    """Format messages as SDK message list, filtering out host messages.

    Host messages are operational notifications that should NOT be sent to the LLM.
    Returns a list of dicts that can be passed to the container/SDK.

    Message type mapping:
    - 'user' → UserMessage (from humans)
    - 'assistant' → AssistantMessage (from LLM)
    - 'system' → SystemMessage (context for LLM)
    - 'tool_result' → Part of conversation history (command outputs, etc.)
    - 'host' → FILTERED OUT (never sent to LLM)
    """
    sdk_messages = []

    for msg in messages:
        # Skip host messages - they're operational, not part of the LLM conversation
        if msg.message_type == "host":
            continue

        sdk_messages.append({
            "message_type": msg.message_type,
            "sender": msg.sender,
            "sender_name": msg.sender_name,
            "content": msg.content,
            "timestamp": msg.timestamp,
            "metadata": msg.metadata,
        })

    return sdk_messages


def strip_internal_tags(text: str) -> str:
    """Remove <internal>...</internal> blocks and trim whitespace."""
    return _INTERNAL_TAG_RE.sub("", text).strip()


def parse_host_tag(text: str) -> tuple[bool, str]:
    """Check if text is wrapped in <host> tags. Returns (is_host, content)."""
    match = _HOST_TAG_RE.match(text)
    if match:
        return True, match.group(1).strip()
    return False, text


def format_outbound(channel: Channel, raw_text: str) -> str:
    """Strip internal tags and optionally prefix with assistant name."""
    text = strip_internal_tags(raw_text)
    if not text:
        return ""
    prefix_name = getattr(channel, "prefix_assistant_name", None)
    prefix = f"{ASSISTANT_NAME}: " if prefix_name is not False else ""
    return f"{prefix}{text}"


async def route_outbound(channels: list[Channel], jid: str, text: str) -> None:
    """Find the appropriate connected channel and send a message."""
    channel = next((c for c in channels if c.owns_jid(jid) and c.is_connected()), None)
    if channel is None:
        raise RuntimeError(f"No channel for JID: {jid}")
    await channel.send_message(jid, text)


def format_tool_preview(tool_name: str, tool_input: dict) -> str:
    """Format a one-line preview of a tool invocation for channel messages.

    Extracts the most relevant detail per tool type so WhatsApp/Telegram
    users see *what* the agent is doing, not just the tool name.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            return f"Bash: {cmd}"
        return "Bash"

    if tool_name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path", "")
        if path:
            # Show just the filename or last 50 chars of path
            if len(path) > 50:
                path = "..." + path[-47:]
            return f"{tool_name}: {path}"
        return tool_name

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        parts = [tool_name]
        if pattern:
            parts.append(f"/{pattern}/")
        if path:
            parts.append(path)
        return " ".join(parts)

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        if pattern:
            return f"Glob: {pattern}"
        return "Glob"

    # Fallback: show first 50 chars of input
    preview = str(tool_input)
    if len(preview) > 50:
        preview = preview[:47] + "..."
    return f"{tool_name}: {preview}" if tool_input else tool_name


def find_channel(channels: list[Channel], jid: str) -> Channel | None:
    """Find the channel that owns a given JID."""
    for c in channels:
        if c.owns_jid(jid):
            return c
    return None
