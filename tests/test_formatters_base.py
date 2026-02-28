"""Tests for OutboundEvent types and BaseFormatter protocol."""

from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter, RenderedMessage
from pynchy.types import OutboundEvent, OutboundEventType


def test_outbound_event_creation():
    event = OutboundEvent(
        type=OutboundEventType.TOOL_TRACE,
        content="running command",
        metadata={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert event.type == OutboundEventType.TOOL_TRACE
    assert event.content == "running command"
    assert event.metadata["tool_name"] == "Bash"


def test_outbound_event_defaults():
    event = OutboundEvent(type=OutboundEventType.TEXT, content="hello")
    assert event.metadata == {}


def test_rendered_message_defaults():
    msg = RenderedMessage(text="hello")
    assert msg.blocks is None
    assert msg.metadata == {}


def test_rendered_message_with_blocks():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    msg = RenderedMessage(text="hi", blocks=blocks)
    assert msg.blocks == blocks


def test_base_formatter_is_abstract():
    import pytest

    with pytest.raises(TypeError):
        BaseFormatter()  # type: ignore[abstract]
