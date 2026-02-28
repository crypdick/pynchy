"""Message formatting and outbound routing."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pynchy.types import Channel, NewMessage

_INTERNAL_TAG_RE = re.compile(r"<internal>([\s\S]*?)</internal>")
_HOST_TAG_RE = re.compile(r"^\s*<host>([\s\S]*?)</host>\s*$")


def _format_internal_match(m: re.Match) -> str:
    """Replace <internal>...</internal> with 🧠 _thought_ (italic)."""
    thought = m.group(1).strip()
    if not thought:
        return ""
    return f"\U0001f9e0 _{thought}_\n"


def format_messages_for_sdk(messages: list[NewMessage]) -> list[dict]:
    """Format messages as SDK message list, filtering out non-conversation messages.

    Returns a list of dicts that can be passed to the container/SDK.

    Message type mapping:
    - 'user' → UserMessage (from humans)
    - 'assistant' → AssistantMessage (from LLM)
    - 'tool_result' → Part of conversation history (command outputs, etc.)
    - 'host' → FILTERED OUT (operational, never sent to LLM)
    - sender='system_notice' → FILTERED OUT (point-in-time worktree notifications
      that go stale; current worktree state is delivered via system_notices in
      agent_runner.py instead)
    """
    sdk_messages = []

    for msg in messages:
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
    """Remove <internal>...</internal> blocks completely and trim whitespace.

    Used during streaming to strip *completed* internal blocks before checking
    for unclosed tags. The final display uses format_internal_tags instead.
    """
    return _INTERNAL_TAG_RE.sub("", text).strip()


def format_internal_tags(text: str) -> str:
    """Transform <internal>...</internal> into 🧠 _thought_ (italic) and trim whitespace."""
    return _INTERNAL_TAG_RE.sub(_format_internal_match, text).strip()


def parse_host_tag(text: str) -> tuple[bool, str]:
    """Check if text is wrapped in <host> tags. Returns (is_host, content)."""
    match = _HOST_TAG_RE.match(text)
    if match:
        return True, match.group(1).strip()
    return False, text


def format_outbound(channel: Channel, raw_text: str) -> str:
    """Format internal tags and optionally prefix with assistant name."""
    text = format_internal_tags(raw_text)
    if not text:
        return ""
    prefix_name = getattr(channel, "prefix_assistant_name", None)
    prefix = "🦞 " if prefix_name is not False else ""
    return f"{prefix}{text}"


def _format_lines(
    lines: list[str],
    *,
    prefix: str,
    max_lines: int = 0,
    max_chars: int = 0,
) -> str:
    """Format lines with a prefix, optionally truncating.

    Used by Edit/Write previews to show content snippets in channel messages.
    max_lines=0 and max_chars=0 mean no limit (default).
    """
    if not lines:
        return ""
    shown = lines[:max_lines] if max_lines > 0 else lines
    remainder = len(lines) - len(shown)
    result_lines = []
    for line in shown:
        if max_chars > 0 and len(line) > max_chars:
            line = line[:max_chars] + "..."
        result_lines.append(f"{prefix} {line}")
    if remainder > 0:
        result_lines.append(f"(+{remainder} more lines)")
    return "\n".join(result_lines)


def _truncate_path(path: str, max_len: int = 150) -> str:
    if len(path) > max_len:
        return "..." + path[-(max_len - 3) :]
    return path


def format_tool_preview(tool_name: str, tool_input: dict) -> str:
    """Format a one-line preview of a tool invocation for channel messages.

    Extracts the most relevant detail per tool type so messaging channel
    users see *what* the agent is doing, not just the tool name.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            return f"Bash:\n```\n{cmd}\n```"
        return "Bash"

    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        if path:
            return f"Read: {_truncate_path(path)}"
        return "Read"

    if tool_name == "Edit":
        path = tool_input.get("file_path", "")
        if not path:
            return "Edit"
        header = f"Edit: {_truncate_path(path)}"
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if not old and not new:
            return header
        diff_lines = []
        if old:
            diff_lines.append(_format_lines(old.splitlines(), prefix="-"))
        if new:
            diff_lines.append(_format_lines(new.splitlines(), prefix="+"))
        return header + "\n```\n" + "\n".join(diff_lines) + "\n```"

    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        if not path:
            return "Write"
        header = f"Write: {_truncate_path(path)}"
        content = tool_input.get("content", "")
        if not content:
            return header
        return header + "\n```\n" + _format_lines(content.splitlines(), prefix="+") + "\n```"

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
