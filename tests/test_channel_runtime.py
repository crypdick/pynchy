from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pluggy
from conftest import NullChannel

from pynchy.plugins import channel_runtime


class _FakeChannel(NullChannel):
    def __init__(self, name: str) -> None:
        self.name = name


class _Hook:
    def __init__(self, channels: list[Any]) -> None:
        self._channels = channels

    def pynchy_create_channel(self, context: Any) -> list[Any]:
        return self._channels


class _PM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, channels: list[Any]) -> None:
        self.hook = _Hook(channels)


def _context() -> channel_runtime.ChannelPluginContext:
    return channel_runtime.ChannelPluginContext(
        on_message_callback=lambda _jid, _msg: None,
        on_chat_metadata_callback=lambda _jid, _ts, _name=None: None,
        workspaces=lambda: {},
        send_message=lambda _jid, _text: None,
    )


def test_load_channels_sorts_by_name() -> None:
    channels = channel_runtime.load_channels(
        _PM([_FakeChannel("zeta"), _FakeChannel("alpha")]), _context()
    )
    assert [ch.name for ch in channels] == ["alpha", "zeta"]


def test_load_channels_returns_empty_when_none_discovered() -> None:
    channels = channel_runtime.load_channels(_PM([None]), _context())
    assert channels == []


def test_resolve_default_channel_returns_none_for_tui_default() -> None:
    assert channel_runtime.resolve_default_channel([_FakeChannel("whatsapp")]) is None


def test_resolve_default_channel_uses_explicit_configured_channel() -> None:
    settings = type(
        "Settings",
        (),
        {
            "command_center": type(
                "CommandCenter", (), {"connection": "connection.whatsapp.primary"}
            )()
        },
    )()
    with patch("pynchy.plugins.channel_runtime.get_settings", return_value=settings):
        selected = channel_runtime.resolve_default_channel(
            [_FakeChannel("connection.whatsapp.primary")]
        )
    assert selected is not None
    assert selected.name == "connection.whatsapp.primary"


def test_resolve_default_channel_returns_none_for_empty_channels() -> None:
    assert channel_runtime.resolve_default_channel([]) is None
