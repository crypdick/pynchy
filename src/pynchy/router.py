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

HOST_PREFIX = "[host]"


def escape_xml(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def format_messages(messages: list[NewMessage]) -> str:
    """Format messages as XML for the agent prompt."""
    lines = [
        f'<message sender="{escape_xml(m.sender_name)}" time="{m.timestamp}">'
        f"{escape_xml(m.content)}</message>"
        for m in messages
    ]
    return f"<messages>\n{chr(10).join(lines)}\n</messages>"


def strip_internal_tags(text: str) -> str:
    """Remove <internal>...</internal> blocks and trim whitespace."""
    return _INTERNAL_TAG_RE.sub("", text).strip()


def parse_host_tag(text: str) -> tuple[bool, str]:
    """Check if text is wrapped in <host> tags. Returns (is_host, content)."""
    match = _HOST_TAG_RE.match(text)
    if match:
        return True, match.group(1).strip()
    return False, text


def format_host_message(text: str) -> str:
    """Format a host message with the standard prefix."""
    return f"{HOST_PREFIX} {text}"


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


def find_channel(channels: list[Channel], jid: str) -> Channel | None:
    """Find the channel that owns a given JID."""
    for c in channels:
        if c.owns_jid(jid):
            return c
    return None
