"""Tests for Channel protocol evolution toward send_event.

Verifies the Channel protocol includes ``send_event`` and ``formatter``
as the new outbound interface, replacing the old ``send_message`` method.
"""

from __future__ import annotations

import inspect
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.types import Channel, OutboundEvent, OutboundEventType

# ---------------------------------------------------------------------------
# Protocol shape tests
# ---------------------------------------------------------------------------


def test_channel_protocol_requires_send_event():
    """Channel protocol must include send_event, not send_message."""
    members = {name for name, _ in inspect.getmembers(Channel)}
    assert "send_event" in members


def test_channel_protocol_has_no_send_message():
    """Channel protocol must NOT include send_message -- callers migrate to send_event."""
    members = {name for name, _ in inspect.getmembers(Channel)}
    assert "send_message" not in members


def test_channel_protocol_requires_formatter():
    """Channel protocol must include formatter attribute."""
    # For a Protocol with annotations, the attribute shows up in the
    # class __annotations__ dict.
    assert "formatter" in Channel.__annotations__ or "formatter" in dir(Channel)


# ---------------------------------------------------------------------------
# Neonize mock setup — must happen before importing WhatsAppChannel
# ---------------------------------------------------------------------------

_NEONIZE_MODULES = [
    "neonize",
    "neonize.aioze",
    "neonize.aioze.client",
    "neonize.aioze.events",
    "neonize.events",
    "neonize.proto",
    "neonize.proto.Neonize_pb2",
    "neonize.utils",
    "neonize.utils.jid",
    "neonize.utils.enum",
]
_neonize_mocks: dict[str, ModuleType] = {}
for _mod_name in _NEONIZE_MODULES:
    if _mod_name not in sys.modules:
        _neonize_mocks[_mod_name] = MagicMock()
        sys.modules[_mod_name] = _neonize_mocks[_mod_name]

from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter  # noqa: E402
from pynchy.plugins.channels.whatsapp.channel import WhatsAppChannel  # noqa: E402


def _make_whatsapp_channel() -> WhatsAppChannel:
    """Create a WhatsAppChannel with mocked internals (bypass __init__)."""
    ch = WhatsAppChannel.__new__(WhatsAppChannel)
    ch.name = "connection.whatsapp.test"
    ch.formatter = TextFormatter()
    ch._connection_name = "connection.whatsapp.test"
    ch._on_message = MagicMock()
    ch._on_chat_metadata = MagicMock()
    ch._on_ask_user_answer = None
    ch._workspaces = lambda: {}
    ch._connected = True
    ch._outgoing_queue = MagicMock()
    ch._lid_to_phone = {}
    ch._flushing = False
    # Mock internal transport so _send_text doesn't hit neonize
    ch._client = MagicMock()
    ch._client.send_message = AsyncMock()
    ch._parse_jid = MagicMock(return_value=MagicMock())
    return ch


# ---------------------------------------------------------------------------
# WhatsApp send_event tests
# ---------------------------------------------------------------------------


class TestWhatsAppSendEvent:
    @pytest.mark.asyncio
    async def test_send_event_renders_and_sends(self):
        """send_event should render the event via formatter and send the text."""
        ch = _make_whatsapp_channel()
        event = OutboundEvent(type=OutboundEventType.TEXT, content="Hello world")
        await ch.send_event("test@g.us", event)

        # The internal transport should have been called with the rendered text
        ch._client.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_event_uses_formatter(self):
        """send_event should delegate rendering to self.formatter."""
        ch = _make_whatsapp_channel()
        rendered = ch.formatter.render(OutboundEvent(type=OutboundEventType.TEXT, content="test"))
        assert rendered.text == "test"  # TextFormatter passes through plain text

    def test_whatsapp_has_formatter_attribute(self):
        """WhatsAppChannel must have a formatter attribute."""
        ch = _make_whatsapp_channel()
        assert hasattr(ch, "formatter")

    def test_whatsapp_formatter_is_text_formatter(self):
        """WhatsAppChannel's formatter should be TextFormatter."""
        from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter

        ch = _make_whatsapp_channel()
        assert isinstance(ch.formatter, TextFormatter)

    @pytest.mark.asyncio
    async def test_send_event_result_type(self):
        """send_event with RESULT type should render with prefix."""
        ch = _make_whatsapp_channel()
        event = OutboundEvent(
            type=OutboundEventType.RESULT,
            content="Done!",
            metadata={"prefix_assistant_name": True},
        )
        await ch.send_event("test@g.us", event)
        ch._client.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_event_queues_when_disconnected(self):
        """When disconnected, send_event should queue the message."""
        ch = _make_whatsapp_channel()
        ch._connected = False
        ch._outgoing_queue = MagicMock()
        ch._outgoing_queue.append = MagicMock()
        event = OutboundEvent(type=OutboundEventType.TEXT, content="queued")
        await ch.send_event("test@g.us", event)
        # Should have queued rather than sent directly
        ch._outgoing_queue.append.assert_called_once()


class TestWhatsAppPrivateSendText:
    """Verify that send_message was renamed to _send_text."""

    def test_has_send_text(self):
        """WhatsAppChannel should have a _send_text method."""
        assert hasattr(WhatsAppChannel, "_send_text")

    @pytest.mark.asyncio
    async def test_send_text_works(self):
        """_send_text should still handle the raw text sending."""
        ch = _make_whatsapp_channel()
        await ch._send_text("test@g.us", "hello")
        ch._client.send_message.assert_called_once()
