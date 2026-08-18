"""Edge behavior for Discord outbound approval delivery."""

from __future__ import annotations

from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from pynchy.plugins.api import OutboundEvent, OutboundEventType
from tests.discord_channel_support import _channel, _FakeSendChannel, _FakeStreamChannel


@pytest.mark.asyncio
async def test_send_event_routes_forum_root_through_post_creation() -> None:
    class _CreatedForumPost(NamedTuple):
        thread: _FakeSendChannel

    class _Forum:
        available_tags: list[object] = []

        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.thread = _FakeSendChannel()

        async def create_thread(self, **kwargs: object) -> _CreatedForumPost:
            self.requests.append(kwargs)
            return _CreatedForumPost(self.thread)

    channel = _channel()
    channel.client = object()
    forum = _Forum()
    channel.resolve_channel = AsyncMock(return_value=forum)  # type: ignore[method-assign]

    await channel.send_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.RESULT, content="reply")
    )

    assert forum.requests[0]["content"] == "reply"
    assert forum.thread.sends == []


@pytest.mark.asyncio
async def test_send_synthetic_user_input_marks_the_discord_message() -> None:
    channel = _channel()
    channel.client = object()
    destination = _FakeSendChannel()
    channel.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await channel.send_event(
        "discord:channel:1",
        OutboundEvent(
            type=OutboundEventType.TEXT,
            content="use native search_skills",
            metadata={"synthetic_user_input": True},
        ),
    )

    assert destination.sends[0][0] == "🦜 use native search_skills"


@pytest.mark.asyncio
async def test_empty_approval_content_does_not_send_a_blank_discord_message() -> None:
    channel = _channel()
    channel.client = object()
    destination = _FakeStreamChannel()
    channel.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await channel.send_event(
        "discord:channel:1",
        OutboundEvent(type=OutboundEventType.APPROVAL, content=""),
    )

    assert destination.sends == []


@pytest.mark.asyncio
async def test_long_approval_content_puts_controls_only_on_the_first_chunk() -> None:
    channel = _channel()
    channel.client = object()
    destination = _FakeStreamChannel()
    channel.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await channel.send_event(
        "discord:channel:1",
        OutboundEvent(
            type=OutboundEventType.APPROVAL,
            content="word " * 1000,
            metadata={"short_id": "approve-1"},
        ),
    )

    assert len(destination.sends) > 1
    assert destination.sends[0][1]["view"] is not None
    assert all("view" not in kwargs for _, kwargs in destination.sends[1:])


@pytest.mark.asyncio
async def test_send_event_reports_discord_resolution_failure() -> None:
    channel = _channel()
    channel.client = object()
    channel.resolve_channel = AsyncMock(side_effect=discord.DiscordException("offline"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="Discord channel resolution failed"):
        await channel.send_event(
            "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi")
        )


@pytest.mark.asyncio
async def test_send_event_reports_forbidden_destination() -> None:
    channel = _channel()
    channel.client = object()
    destination = _FakeStreamChannel()
    channel.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]
    forbidden = discord.Forbidden(MagicMock(status=403, reason="blocked"), "blocked")

    with (
        patch("pynchy.plugins.channels.discord._channel.send_text", side_effect=forbidden),
        pytest.raises(OSError, match="Discord send forbidden"),
    ):
        await channel.send_event(
            "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi")
        )


@pytest.mark.asyncio
async def test_post_event_returns_none_when_discord_rejects_preview() -> None:
    channel = _channel()
    channel.client = object()
    channel.resolve_channel = AsyncMock(side_effect=discord.DiscordException("offline"))  # type: ignore[method-assign]

    result = await channel.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi")
    )

    assert result is None


@pytest.mark.asyncio
async def test_post_event_skips_forum_root_preview() -> None:
    class _ForumRoot:
        available_tags: list[object] = []

    channel = _channel()
    channel.client = object()
    channel.resolve_channel = AsyncMock(return_value=_ForumRoot())  # type: ignore[method-assign]

    assert (
        await channel.post_event(
            "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="preview")
        )
        is None
    )


@pytest.mark.asyncio
async def test_send_event_rejects_an_unsupported_destination() -> None:
    channel = _channel()
    channel.client = object()
    channel.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="does not support sending"):
        await channel.send_event(
            "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi")
        )
