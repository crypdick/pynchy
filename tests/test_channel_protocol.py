"""Public channel protocol contract tests."""

from __future__ import annotations

import inspect

from pynchy.types import Channel


def test_channel_protocol_requires_send_event() -> None:
    """Every channel exposes the event-based outbound protocol."""
    members = {name for name, _ in inspect.getmembers(Channel)}
    assert "send_event" in members


def test_channel_protocol_includes_an_event_formatter() -> None:
    """Channels expose a formatter for their public outbound event contract."""
    assert "formatter" in Channel.__annotations__ or "formatter" in dir(Channel)
