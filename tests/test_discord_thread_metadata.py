"""Tests for Discord forum-thread metadata updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.discord_channel_support import _channel


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
