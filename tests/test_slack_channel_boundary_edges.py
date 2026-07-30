"""Public SlackChannel behavior at outbound and lookup boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.plugins.api import OutboundEvent, OutboundEventType
from pynchy.plugins.channels.slack import SlackChannel

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeSlackClient:
    def __init__(self) -> None:
        self.chat_postMessage = AsyncMock(return_value={"ts": "123.456"})
        self.chat_update = AsyncMock()
        self.reactions_add = AsyncMock()
        self.users_info = AsyncMock()
        self.conversations_info = AsyncMock()


class _FakeSlackApp:
    def __init__(self) -> None:
        self.client = _FakeSlackClient()


def _token(prefix: str) -> str:
    return f"{prefix}-fixture"


def _channel(*, on_reaction: Callable[[str, str, str, str], None] | None = None) -> SlackChannel:
    channel = SlackChannel(
        connection_name="test",
        bot_token=_token("xoxb"),
        app_token=_token("xapp"),
        chat_names=["general"],
        assistant_name="pynchy",
        allow_create=False,
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        on_reaction=on_reaction,
    )
    app = _FakeSlackApp()
    channel.slack_app = app
    channel.register_allowed_channel("general", "C123")
    return channel


@pytest.fixture
def slack_channel() -> SlackChannel:
    return _channel()


def test_require_slack_app_reports_an_uninitialized_connection() -> None:
    channel = _channel()
    channel.slack_app = None

    with pytest.raises(RuntimeError, match="Slack app is not initialized"):
        channel.require_slack_app()


def test_track_slack_ts_prunes_expired_entries_before_recording_new_activity() -> None:
    channel = _channel()
    for index in range(500):
        assert channel.track_slack_ts(f"old-{index}", now=0.0) is False

    assert channel.track_slack_ts("fresh", now=1_000.0, ttl_seconds=10.0) is False
    assert channel.track_slack_ts("fresh", now=1_000.0, ttl_seconds=10.0) is True


@pytest.mark.asyncio
async def test_send_event_chunks_plain_text_without_blocks(slack_channel: SlackChannel) -> None:
    slack_channel.formatter.render = MagicMock(return_value=MagicMock(text="a" * 6_001, blocks=[]))

    await slack_channel.send_event(
        "slack:C123",
        OutboundEvent(type=OutboundEventType.TEXT, content="long", metadata={}),
    )

    calls = slack_channel.require_slack_app().client.chat_postMessage.await_args_list
    assert len(calls) == 3
    assert all("blocks" not in call.kwargs for call in calls)
    assert "".join(call.kwargs["text"] for call in calls) == "a" * 6_001


@pytest.mark.asyncio
async def test_post_event_returns_none_when_slack_does_not_return_a_timestamp(
    slack_channel: SlackChannel,
) -> None:
    slack_channel.formatter.render = MagicMock(return_value=MagicMock(text="text", blocks=[]))
    slack_channel.require_slack_app().client.chat_postMessage.return_value = {}

    result = await slack_channel.post_event(
        "slack:C123",
        OutboundEvent(type=OutboundEventType.TEXT, content="text", metadata={}),
    )

    assert result is None
    assert (
        "blocks" not in slack_channel.require_slack_app().client.chat_postMessage.call_args.kwargs
    )


@pytest.mark.asyncio
async def test_update_event_posts_plain_text_without_blocks(slack_channel: SlackChannel) -> None:
    slack_channel.formatter.render = MagicMock(return_value=MagicMock(text="text", blocks=[]))

    await slack_channel.update_event(
        "slack:C123",
        "123.456",
        OutboundEvent(type=OutboundEventType.TEXT, content="text", metadata={}),
    )

    assert slack_channel.require_slack_app().client.chat_update.call_args.kwargs == {
        "channel": "C123",
        "ts": "123.456",
        "text": "text",
    }


def test_emit_reaction_invokes_the_optional_callback() -> None:
    callback = MagicMock()
    channel = _channel(on_reaction=callback)

    channel.emit_reaction("slack:C123", "123.456", "U1", "eyes")

    callback.assert_called_once_with("slack:C123", "123.456", "U1", "eyes")


def test_emit_reaction_is_a_no_op_without_a_callback() -> None:
    _channel().emit_reaction("slack:C123", "123.456", "U1", "eyes")


def test_register_inbound_handlers_delegates_to_event_collaborator() -> None:
    channel = _channel()

    with patch.object(type(channel.events), "register_handlers", MagicMock()) as register:
        channel.register_inbound_handlers()

    register.assert_called_once_with()


@pytest.mark.asyncio
async def test_send_reaction_extracts_ids_and_normalizes_emoji(slack_channel: SlackChannel) -> None:
    await slack_channel.send_reaction("slack:C123", "slack-assistant-123.456", "U1", "👀")
    await slack_channel.send_reaction("slack:C123", "slack-234.567", "U1", ":lobster:")

    calls = slack_channel.require_slack_app().client.reactions_add.await_args_list
    assert [call.kwargs for call in calls] == [
        {"channel": "C123", "timestamp": "123.456", "name": "eyes"},
        {"channel": "C123", "timestamp": "234.567", "name": "lobster"},
    ]


@pytest.mark.asyncio
async def test_send_reaction_ignores_unrelated_ids_and_unowned_channels(
    slack_channel: SlackChannel,
) -> None:
    await slack_channel.send_reaction("slack:C123", "message-1", "U1", "eyes")
    await slack_channel.send_reaction("slack:OTHER", "slack-1.2", "U1", "eyes")
    slack_channel.slack_app = None
    await slack_channel.send_reaction("slack:C123", "slack-1.2", "U1", "eyes")

    slack_channel.slack_app = _FakeSlackApp()
    slack_channel.require_slack_app().client.reactions_add = AsyncMock(
        side_effect=RuntimeError("Slack unavailable")
    )
    await slack_channel.send_reaction("slack:C123", "slack-1.2", "U1", "eyes")
    slack_channel.require_slack_app().client.reactions_add.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_inbound_since_requires_a_cursor(slack_channel: SlackChannel) -> None:
    slack_channel.history.fetch_missed_messages_with_watermark = AsyncMock()

    result = await slack_channel.fetch_inbound_since("slack:C123", "")

    assert result.messages == []
    slack_channel.history.fetch_missed_messages_with_watermark.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_ask_user_posts_questions_and_returns_message_id() -> None:
    channel = _channel()

    result = await channel.send_ask_user(
        "slack:C123",
        "request-1",
        [{"question": "Deploy?", "options": [{"label": "Yes", "value": "yes"}]}],
    )

    assert result == "123.456"
    call = channel.require_slack_app().client.chat_postMessage.await_args.kwargs
    assert call["channel"] == "C123"
    assert call["text"] == "Question: Deploy?"
    assert call["blocks"]


@pytest.mark.asyncio
async def test_send_ask_user_skips_unavailable_slack_app() -> None:
    channel = _channel()
    channel.slack_app = None

    assert await channel.send_ask_user("slack:C123", "request-1", []) is None


@pytest.mark.asyncio
async def test_name_resolution_falls_back_without_app_or_when_lookup_fails() -> None:
    channel = _channel()
    channel.slack_app = None
    assert await channel.resolve_user_name("U1") == "U1"
    assert await channel.resolve_channel_name("C1") == "C1"

    channel = _channel()
    channel.require_slack_app().client.users_info = AsyncMock(side_effect=RuntimeError("offline"))
    channel.require_slack_app().client.conversations_info = AsyncMock(
        side_effect=RuntimeError("offline")
    )
    assert await channel.resolve_user_name("U1") == "U1"
    assert await channel.resolve_channel_name("C1") == "C1"


@pytest.mark.asyncio
async def test_name_resolution_caches_successful_provider_results() -> None:
    channel = _channel()
    channel.require_slack_app().client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Ada"}}}
    )
    channel.require_slack_app().client.conversations_info = AsyncMock(
        return_value={"channel": {"name": "general"}}
    )

    assert await channel.resolve_user_name("U1") == "Ada"
    assert await channel.resolve_user_name("U1") == "Ada"
    assert await channel.resolve_channel_name("C1") == "general"
    assert await channel.resolve_channel_name("C1") == "general"
    channel.require_slack_app().client.users_info.assert_awaited_once_with(user="U1")
    channel.require_slack_app().client.conversations_info.assert_awaited_once_with(channel="C1")
