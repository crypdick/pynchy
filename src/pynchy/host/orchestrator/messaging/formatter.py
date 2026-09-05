"""Message formatting and outbound routing."""

from __future__ import annotations

import re
from collections.abc import (  # noqa: TC003 - beartype resolves formatter annotations at runtime.
    Callable,
)
from typing import Any

from pynchy.plugins.api import (
    NewMessage,  # noqa: TC001 - beartype resolves formatter annotations at runtime.
)

_INTERNAL_TAG_RE = re.compile(r"<internal>([\s\S]*?)</internal>")
_HOST_TAG_RE = re.compile(r"^\s*<host>([\s\S]*?)</host>\s*$")
_ATTACHMENT_CONTEXT_FIELDS = (
    "filename",
    "url",
    "content_type",
    "size",
    "description",
    "spoiler",
)


def _format_internal_match(m: re.Match[str]) -> str:
    """Format <internal>...</internal> as 🧠 _thought_ (italic)."""
    thought = m.group(1).strip()
    if not thought:
        return ""
    return f"\U0001f9e0 _{thought}_\n"


def _selected_fields(value: object, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in fields if value.get(field) is not None}


def _attachments_context(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        projected
        for attachment in value
        if (projected := _selected_fields(attachment, _ATTACHMENT_CONTEXT_FIELDS))
    ]


def _agent_context(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Project stored channel metadata into provider-neutral conversation context."""
    context: dict[str, Any] = {}
    if attachments := _attachments_context(metadata.get("attachments")):
        context["attachments"] = attachments

    reply = {}
    if sender := metadata.get("reply_to_sender"):
        reply["sender"] = sender
    if content := metadata.get("reply_to_text"):
        reply["content"] = content
    if reply:
        context["reply"] = reply

    forwarded_messages = []
    raw_forwarded = metadata.get("forwarded_messages")
    if isinstance(raw_forwarded, list):
        for raw_message in raw_forwarded:
            message = _selected_fields(raw_message, ("content", "created_at"))
            if isinstance(raw_message, dict) and (
                attachments := _attachments_context(raw_message.get("attachments"))
            ):
                message["attachments"] = attachments
            if message:
                forwarded_messages.append(message)
    if forwarded_messages:
        context["forwarded_messages"] = forwarded_messages

    return context or None


def format_messages_for_sdk(messages: list[NewMessage]) -> list[dict[str, Any]]:
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

        metadata = msg.metadata or {}
        # Agent input is an explicit semantic projection. Routing, authority, provider,
        # and synthetic provenance remain host-only instead of becoming prompt text.
        synthetic_user_input = metadata.get("synthetic_user_input") is True

        sdk_messages.append(
            {
                "message_type": msg.message_type,
                "sender": "user" if synthetic_user_input else msg.sender,
                "sender_name": "User" if synthetic_user_input else msg.sender_name,
                "content": msg.content,
                "timestamp": msg.timestamp,
                "context": _agent_context(metadata),
            }
        )

    return sdk_messages


def format_internal_tags(text: str) -> str:
    """Transform <internal>...</internal> into 🧠 _thought_ (italic) and trim whitespace."""
    return _INTERNAL_TAG_RE.sub(_format_internal_match, text).strip()


def parse_host_tag(text: str) -> tuple[bool, str]:
    """Check if text is wrapped in <host> tags. Returns (is_host, content)."""
    match = _HOST_TAG_RE.match(text)
    if match:
        return True, match.group(1).strip()
    return False, text


def _format_lines(lines: list[str], *, prefix: str) -> str:
    """Prefix each line for an Edit/Write diff preview in channel messages."""
    return "\n".join(f"{prefix} {line}" for line in lines)


def _truncate_path(path: str, max_len: int = 150) -> str:
    if len(path) > max_len:
        return "..." + path[-(max_len - 3) :]
    return path


def _preview_bash(tool_input: dict[str, Any]) -> str:
    cmd = tool_input.get("command", "")
    if cmd:
        return f"Bash:\n```\n{cmd}\n```"
    return "Bash"


def _preview_read(tool_input: dict[str, Any]) -> str:
    path = tool_input.get("file_path", "")
    if path:
        return f"Read: {_truncate_path(path)}"
    return "Read"


def _preview_edit(tool_input: dict[str, Any]) -> str:
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


def _preview_write(tool_input: dict[str, Any]) -> str:
    path = tool_input.get("file_path", "")
    if not path:
        return "Write"
    header = f"Write: {_truncate_path(path)}"
    content = tool_input.get("content", "")
    if not content:
        return header
    return header + "\n```\n" + _format_lines(content.splitlines(), prefix="+") + "\n```"


def _preview_grep(tool_input: dict[str, Any]) -> str:
    pattern = tool_input.get("pattern", "")
    path = tool_input.get("path", "")
    parts = ["Grep"]
    if pattern:
        parts.append(f"/{pattern}/")
    if path:
        parts.append(path)
    return " ".join(parts)


def _preview_glob(tool_input: dict[str, Any]) -> str:
    pattern = tool_input.get("pattern", "")
    if pattern:
        return f"Glob: {pattern}"
    return "Glob"


def _preview_truncated_field(tool_name: str, tool_input: dict[str, Any], key: str) -> str:
    """Shared by WebFetch/WebSearch: show a length-capped field value."""
    value = tool_input.get(key, "")
    if not value:
        return tool_name
    if len(value) > 150:
        value = value[:147] + "..."
    return f"{tool_name}: {value}"


def _preview_task(tool_input: dict[str, Any]) -> str:
    desc = tool_input.get("description", "")
    if desc:
        return f"Task: {desc}"
    return "Task"


def _preview_ask_user_question(tool_input: dict[str, Any]) -> str:
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


def _preview_fallback(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Show the first 150 chars of the raw input for tools with no dedicated formatter."""
    preview = str(tool_input)
    if len(preview) > 150:
        preview = preview[:147] + "..."
    return f"{tool_name}: {preview}" if tool_input else tool_name


_TOOL_PREVIEW_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "Bash": _preview_bash,
    "Read": _preview_read,
    "Edit": _preview_edit,
    "Write": _preview_write,
    "Grep": _preview_grep,
    "Glob": _preview_glob,
    "WebFetch": lambda ti: _preview_truncated_field("WebFetch", ti, "url"),
    "WebSearch": lambda ti: _preview_truncated_field("WebSearch", ti, "query"),
    "Task": _preview_task,
    "AskUserQuestion": _preview_ask_user_question,
}


def format_tool_preview(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a one-line preview of a tool invocation for channel messages.

    Extracts the most relevant detail per tool type so messaging channel
    users see *what* the agent is doing, not just the tool name.
    """
    formatter = _TOOL_PREVIEW_FORMATTERS.get(tool_name)
    if formatter is not None:
        return formatter(tool_input)
    return _preview_fallback(tool_name, tool_input)
