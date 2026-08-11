"""Tests for Discord forum-thread metadata updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.discord_channel_support import _channel


@pytest.mark.asyncio
async def test_pins_one_linear_issue_link_without_duplicates():
    class _Message:
        def __init__(self, content: str) -> None:
            self.content = content
            self.pinned = False

        async def pin(self) -> None:
            self.pinned = True

    class _Thread:
        def __init__(self) -> None:
            self.sent: list[_Message] = []

        async def pins(self) -> list[_Message]:
            return [message for message in self.sent if message.pinned]

        async def send(self, content: str) -> _Message:
            message = _Message(content)
            self.sent.append(message)
            return message

    ch = _channel()
    thread = _Thread()
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    await ch.ensure_thread_link_pinned("discord:channel:456", "https://linear.app/acme/issue/PYN-1")
    await ch.ensure_thread_link_pinned("discord:channel:456", "https://linear.app/acme/issue/PYN-1")

    assert [message.content for message in thread.sent] == [
        "Linear issue: https://linear.app/acme/issue/PYN-1"
    ]
    assert thread.sent[0].pinned is True


@pytest.mark.asyncio
async def test_forum_guidelines_keep_operator_text_and_replace_project_link():
    class _Forum:
        available_tags: list[object] = []

        def __init__(self) -> None:
            self.topic = "Use clear titles.\nLinear project: https://linear.app/acme/project/old"
            self.edits: list[str] = []

        async def edit(self, *, topic: str) -> None:
            self.topic = topic
            self.edits.append(topic)

    ch = _channel()
    forum = _Forum()
    ch.resolve_channel = AsyncMock(return_value=forum)  # type: ignore[method-assign]

    await ch.ensure_forum_guidelines_linked(
        "discord:channel:123", "https://linear.app/acme/project/current"
    )
    await ch.ensure_forum_guidelines_linked(
        "discord:channel:123", "https://linear.app/acme/project/current"
    )

    assert (
        forum.topic == "Use clear titles.\nLinear project: https://linear.app/acme/project/current"
    )
    assert forum.edits == [forum.topic]


@pytest.mark.asyncio
async def test_thread_metadata_rejects_targets_that_cannot_persist_it():
    ch = _channel()

    ch.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="sending a pinned link"):
        await ch.ensure_thread_link_pinned(
            "discord:channel:456", "https://linear.app/acme/issue/PYN-1"
        )

    forum = MagicMock(available_tags=[])
    forum.edit = None
    ch.resolve_channel = AsyncMock(return_value=forum)  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="posting guidelines"):
        await ch.ensure_forum_guidelines_linked(
            "discord:channel:123", "https://linear.app/acme/project/current"
        )


@pytest.mark.asyncio
async def test_thread_link_requires_a_pinnable_discord_message():
    ch = _channel()
    thread = MagicMock()
    thread.pins = AsyncMock(return_value=[])
    thread.send = AsyncMock(return_value=object())
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="does not support pinning"):
        await ch.ensure_thread_link_pinned(
            "discord:channel:456", "https://linear.app/acme/issue/PYN-1"
        )


@pytest.mark.asyncio
async def test_forum_guidelines_ignore_non_forum_channels():
    ch = _channel()
    ch.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]

    await ch.ensure_forum_guidelines_linked(
        "discord:channel:123", "https://linear.app/acme/project/current"
    )


@pytest.mark.asyncio
async def test_thread_kind_resolves_parent_and_rejects_missing_forum_tag():
    ch = _channel()
    thread = MagicMock()
    thread.parent = None
    thread.parent_id = 789
    parent = MagicMock()
    parent.available_tags = []
    ch.client = MagicMock()
    ch.client.get_channel.return_value = parent
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="lacks required post tag"):
        await ch.set_thread_kind("discord:channel:456", "automation")

    ch.client.get_channel.assert_called_once_with(789)


@pytest.mark.asyncio
async def test_thread_kind_ignores_a_non_forum_thread_without_parent():
    ch = _channel()
    thread = MagicMock()
    thread.parent = None
    thread.parent_id = None
    ch.client = MagicMock()
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    await ch.set_thread_kind("discord:channel:456", "automation")

    ch.client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_thread_kind_keeps_matching_existing_tag():
    ch = _channel()
    tag = MagicMock()
    tag.name = "automation"
    tag.id = 10
    parent = MagicMock()
    parent.available_tags = [tag]
    thread = MagicMock()
    thread.parent = parent
    thread.applied_tags = [tag]
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    await ch.set_thread_kind("discord:channel:456", "automation")

    thread.edit.assert_not_called()


@pytest.mark.asyncio
async def test_thread_kind_rejects_targets_without_edit_support():
    ch = _channel()
    tag = MagicMock()
    tag.name = "automation"
    parent = MagicMock()
    parent.available_tags = [tag]
    thread = MagicMock()
    thread.parent = parent
    thread.applied_tags = []
    thread.edit = None
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="forum post tags"):
        await ch.set_thread_kind("discord:channel:456", "automation")


@pytest.mark.asyncio
async def test_thread_title_rejects_targets_without_edit_support():
    ch = _channel()
    thread = MagicMock()
    thread.name = "old title"
    thread.edit = None
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="thread titles"):
        await ch.set_thread_title("discord:channel:456", "new title")
