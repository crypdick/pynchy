from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pynchy import channel_runtime


@dataclass
class _FakeChannel:
    name: str


class _Hook:
    def __init__(self, channels: list[Any]) -> None:
        self._channels = channels

    def pynchy_create_channel(self, context: Any) -> list[Any]:  # noqa: ARG002
        return self._channels


class _PM:
    def __init__(self, channels: list[Any]) -> None:
        self.hook = _Hook(channels)


def _context() -> channel_runtime.ChannelPluginContext:
    return channel_runtime.ChannelPluginContext(
        on_message_callback=lambda _jid, _msg: None,
        on_chat_metadata_callback=lambda _jid, _ts, _name=None: None,
        registered_groups=lambda: {},
        send_message=lambda _jid, _text: None,
    )


def test_load_channels_sorts_by_name() -> None:
    channels = channel_runtime.load_channels(
        _PM([_FakeChannel("zeta"), _FakeChannel("alpha")]), _context()
    )
    assert [ch.name for ch in channels] == ["alpha", "zeta"]


def test_load_channels_raises_when_none_discovered() -> None:
    with pytest.raises(RuntimeError, match="No channel plugins were discovered"):
        channel_runtime.load_channels(_PM([None]), _context())


def test_resolve_default_channel_uses_config_fallback() -> None:
    selected = channel_runtime.resolve_default_channel([_FakeChannel("whatsapp")])
    assert selected.name == "whatsapp"
