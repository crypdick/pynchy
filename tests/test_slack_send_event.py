"""Tests for SlackChannel.send_event, post_event, and update_event."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.types import OutboundEvent, OutboundEventType


@pytest.fixture
def slack_channel():
    """Create a SlackChannel with mocked Slack app."""
    from pynchy.plugins.channels.slack._channel import SlackChannel

    ch = SlackChannel(
        connection_name="test",
        bot_token="xoxb-test",
        app_token="xapp-test",
        chat_names=["general"],
        allow_create=False,
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
    )
    ch._app = MagicMock()
    ch._app.client = MagicMock()
    ch._app.client.chat_postMessage = AsyncMock(return_value={"ts": "123.456"})
    ch._app.client.chat_update = AsyncMock()
    ch._connected = True
    ch._allowed_channel_ids = {"C123"}
    return ch


@pytest.mark.asyncio
async def test_send_event_posts_text(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello world",
        metadata={"prefix_assistant_name": False},
    )
    await slack_channel.send_event("slack:C123", event)
    slack_channel._app.client.chat_postMessage.assert_called_once()


@pytest.mark.asyncio
async def test_send_event_skips_non_owned_jid(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello world",
        metadata={"prefix_assistant_name": False},
    )
    await slack_channel.send_event("slack:WRONG", event)
    slack_channel._app.client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_send_event_skips_when_no_app(slack_channel):
    slack_channel._app = None
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello world",
        metadata={"prefix_assistant_name": False},
    )
    await slack_channel.send_event("slack:C123", event)
    # No exception raised — method returns early


@pytest.mark.asyncio
async def test_send_event_splits_long_text(slack_channel):
    """Long text without blocks should be split into chunks."""
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="a" * 6000,
        metadata={"prefix_assistant_name": False},
    )
    await slack_channel.send_event("slack:C123", event)
    assert slack_channel._app.client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_send_event_with_blocks_sends_blocks(slack_channel):
    """When the formatter produces blocks, send_event should pass them through."""
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello world",
        metadata={"prefix_assistant_name": False},
    )
    # Patch the formatter to return blocks
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Hello world"}}]
    slack_channel.formatter.render = MagicMock(
        return_value=MagicMock(text="Hello world", blocks=blocks)
    )
    await slack_channel.send_event("slack:C123", event)
    call_kwargs = slack_channel._app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["blocks"] == blocks
    assert call_kwargs["text"] == "Hello world"


@pytest.mark.asyncio
async def test_post_event_returns_ts(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.TEXT, content="streaming", metadata={"cursor": True}
    )
    ts = await slack_channel.post_event("slack:C123", event)
    assert ts == "123.456"


@pytest.mark.asyncio
async def test_post_event_returns_none_for_wrong_jid(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.TEXT, content="streaming", metadata={"cursor": True}
    )
    ts = await slack_channel.post_event("slack:WRONG", event)
    assert ts is None


@pytest.mark.asyncio
async def test_post_event_returns_none_when_no_app(slack_channel):
    slack_channel._app = None
    event = OutboundEvent(
        type=OutboundEventType.TEXT, content="streaming", metadata={"cursor": True}
    )
    ts = await slack_channel.post_event("slack:C123", event)
    assert ts is None


@pytest.mark.asyncio
async def test_post_event_passes_blocks_when_present(slack_channel):
    """post_event should include blocks in the API call if the formatter produces them."""
    event = OutboundEvent(type=OutboundEventType.TEXT, content="text", metadata={})
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "text"}}]
    slack_channel.formatter.render = MagicMock(return_value=MagicMock(text="text", blocks=blocks))
    await slack_channel.post_event("slack:C123", event)
    call_kwargs = slack_channel._app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["blocks"] == blocks


@pytest.mark.asyncio
async def test_update_event_calls_chat_update(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.TEXT, content="final text", metadata={"cursor": False}
    )
    await slack_channel.update_event("slack:C123", "123.456", event)
    slack_channel._app.client.chat_update.assert_called_once()


@pytest.mark.asyncio
async def test_update_event_skips_non_owned_jid(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.TEXT, content="final text", metadata={"cursor": False}
    )
    await slack_channel.update_event("slack:WRONG", "123.456", event)
    slack_channel._app.client.chat_update.assert_not_called()


@pytest.mark.asyncio
async def test_update_event_skips_when_no_app(slack_channel):
    slack_channel._app = None
    event = OutboundEvent(
        type=OutboundEventType.TEXT, content="final text", metadata={"cursor": False}
    )
    await slack_channel.update_event("slack:C123", "123.456", event)
    # No exception raised — method returns early


@pytest.mark.asyncio
async def test_update_event_passes_blocks_when_present(slack_channel):
    """update_event should include blocks in the API call if the formatter produces them."""
    event = OutboundEvent(type=OutboundEventType.TEXT, content="text", metadata={})
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "text"}}]
    slack_channel.formatter.render = MagicMock(return_value=MagicMock(text="text", blocks=blocks))
    await slack_channel.update_event("slack:C123", "123.456", event)
    call_kwargs = slack_channel._app.client.chat_update.call_args.kwargs
    assert call_kwargs["blocks"] == blocks
    assert call_kwargs["ts"] == "123.456"


@pytest.mark.asyncio
async def test_update_event_omits_blocks_when_none(slack_channel):
    """update_event should not include blocks key when formatter returns None blocks."""
    event = OutboundEvent(type=OutboundEventType.TEXT, content="text", metadata={})
    await slack_channel.update_event("slack:C123", "123.456", event)
    call_kwargs = slack_channel._app.client.chat_update.call_args.kwargs
    assert "blocks" not in call_kwargs


@pytest.mark.asyncio
async def test_formatter_is_text_formatter(slack_channel):
    """SlackChannel should use TextFormatter by default."""
    from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter

    assert isinstance(slack_channel.formatter, TextFormatter)
