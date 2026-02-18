"""Message formatting and outbound routing."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pynchy.types import Channel, NewMessage

_INTERNAL_TAG_RE = re.compile(r"<internal>[\s\S]*?</internal>")
_HOST_TAG_RE = re.compile(r"^\s*<host>([\s\S]*?)</host>\s*$")


def format_messages_for_sdk(messages: list[NewMessage]) -> list[dict]:
    """Format messages as SDK message list, filtering out host messages.

    Host messages are operational notifications that should NOT be sent to the LLM.
    Returns a list of dicts that can be passed to the container/SDK.

    Message type mapping:
    - 'user' → UserMessage (from humans)
    - 'assistant' → AssistantMessage (from LLM)
    - 'system' → SystemMessage (context for LLM, stored in DB for persistent context)
    - 'tool_result' → Part of conversation history (command outputs, etc.)
    - 'host' → FILTERED OUT (never sent to LLM)

    Note on system_notices:
        Ephemeral system context (git warnings, etc.) is handled separately via
        system_prompt in ContainerInput. This function only handles persisted messages.
    """
    sdk_messages = []

    for msg in messages:
        # Skip host messages - they're operational, not part of the LLM conversation
        if msg.message_type == "host":
            continue

        sdk_messages.append(
            {
                "message_type": msg.message_type,
                "sender": msg.sender,
                "sender_name": msg.sender_name,
                "content": msg.content,
                "timestamp": msg.timestamp,
                "metadata": msg.metadata,
            }
        )

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
    prefix = "🦞 " if prefix_name is not False else ""
    return f"{prefix}{text}"


def format_tool_preview(tool_name: str, tool_input: dict) -> str:
    """Format a one-line preview of a tool invocation for channel messages.

    Extracts the most relevant detail per tool type so messaging channel
    users see *what* the agent is doing, not just the tool name.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            if len(cmd) > 180:
                cmd = cmd[:177] + "..."
            return f"Bash: {cmd}"
        return "Bash"

    if tool_name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path", "")
        if path:
            if len(path) > 150:
                path = "..." + path[-147:]
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

    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if url:
            if len(url) > 150:
                url = url[:147] + "..."
            return f"WebFetch: {url}"
        return "WebFetch"

    if tool_name == "WebSearch":
        query = tool_input.get("query", "")
        if query:
            if len(query) > 150:
                query = query[:147] + "..."
            return f"WebSearch: {query}"
        return "WebSearch"

    if tool_name == "Task":
        desc = tool_input.get("description", "")
        if desc:
            return f"Task: {desc}"
        return "Task"

    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            parts = []
            for q in questions:
                text = q.get("question", "") if isinstance(q, dict) else ""
                if text:
                    parts.append(text)
            if parts:
                return "Asking: " + " | ".join(parts)
        return "AskUserQuestion"

    # Fallback: show first 150 chars of input
    preview = str(tool_input)
    if len(preview) > 150:
        preview = preview[:147] + "..."
    return f"{tool_name}: {preview}" if tool_input else tool_name
