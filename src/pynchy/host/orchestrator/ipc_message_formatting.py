"""Formatting shared by warm-session IPC delivery."""

from __future__ import annotations

from typing import Any


def format_messages_for_ipc(
    messages: list[dict[str, Any]], system_notices: list[str] | None = None
) -> str:
    """Format messages and notices as the XML accepted by a warm container."""
    parts: list[str] = []

    if system_notices:
        notice_lines = "\n".join(f"- {notice}" for notice in system_notices)
        parts.append(f"<system_notices>\n{notice_lines}\n</system_notices>")

    if messages:
        message_lines = [_format_message(message) for message in messages]
        parts.append(f"<messages>\n{chr(10).join(message_lines)}\n</messages>")

    return "\n".join(parts)


def _format_message(message: dict[str, Any]) -> str:
    sender_name = _escape_xml(message.get("sender_name", "Unknown"))
    timestamp = message.get("timestamp", "")
    content = _escape_xml(message.get("content", ""))
    return f'<message sender="{sender_name}" time="{timestamp}">{content}</message>'


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
