"""Helpers for parsing connection/chat references in config strings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionRef:
    platform: str
    name: str


@dataclass(frozen=True)
class ChatRef(ConnectionRef):
    chat: str


def parse_connection_ref(ref: str | None) -> ConnectionRef | None:
    """Parse ``connection.<platform>.<name>`` into a ConnectionRef."""
    if not ref:
        return None
    parts = ref.split(".")
    if len(parts) < 3:
        return None
    if parts[0] != "connection":
        return None
    platform, name = parts[1], parts[2]
    if not platform or not name:
        return None
    return ConnectionRef(platform=platform, name=name)


def parse_chat_ref(ref: str | None) -> ChatRef | None:
    """Parse ``connection.<platform>.<name>.chat.<chat>`` into a ChatRef."""
    if not ref:
        return None
    parts = ref.split(".")
    if len(parts) < 5:
        return None
    if parts[0] != "connection" or parts[3] != "chat":
        return None
    platform, name = parts[1], parts[2]
    chat = ".".join(parts[4:])
    if not platform or not name or not chat:
        return None
    return ChatRef(platform=platform, name=name, chat=chat)


def connection_ref_from_parts(platform: str, name: str) -> str:
    return f"connection.{platform}.{name}"


def chat_ref_from_parts(platform: str, name: str, chat: str) -> str:
    return f"connection.{platform}.{name}.chat.{chat}"


def channel_platform_from_name(channel_name: str | None) -> str | None:
    """Return platform name from a channel instance name."""
    if not channel_name:
        return None
    if channel_name.startswith("connection."):
        parts = channel_name.split(".")
        if len(parts) >= 3:
            return parts[1]
    return channel_name
