"""Slack ↔ pynchy JID conversion helpers."""

from __future__ import annotations

JID_PREFIX = "slack:"


def _jid(channel_id: str) -> str:
    """Convert a Slack channel ID to a pynchy JID."""
    return f"{JID_PREFIX}{channel_id}"


def _channel_id_from_jid(jid: str) -> str:
    """Extract the Slack channel ID from a pynchy JID."""
    return jid.removeprefix(JID_PREFIX)
