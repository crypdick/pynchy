"""Tests for DiscordChannel outbound behavior and protocol conformance.

The discord.py client is faked; these cover the channel's own logic (jid
ownership, chunked sending with safe mention defaults, reaction id handling,
history catch-up filtering) rather than the gateway glue in _lifecycle.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin
from tests.discord_channel_support import (
    _channel,
    _DirectMessageClient,
    _FakeStreamChannel,
    _FakeTypingChannel,
    _FakeUser,
    _HistoryAuthor,
    _HistoryChannel,
    _HistoryMessage,
)

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"


async def _approval_view(
    *,
    callback: MagicMock | None,
) -> tuple[DiscordChannel, object]:
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV, dm_policy="open"
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _msg: None,
        on_chat_metadata=lambda _jid, _ts, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        on_approval_decision=callback,
    )
    ch.client = object()
    destination = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]
    await ch.send_event(
        "discord:direct:42",
        OutboundEvent(
            type=OutboundEventType.APPROVAL,
            content="Approval required",
            metadata={"short_id": "js"},
        ),
    )
    return ch, destination.sends[0][1]["view"]


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = "42"
    interaction.user.bot = False
    interaction.user.roles = []
    interaction.channel.id = "42"
    interaction.channel.parent = None
    interaction.channel.parent_id = None
    interaction.channel.name = None
    interaction.guild = None
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def test_interaction_access_allows_registered_workspace_thread():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"pynchy": {"name": "pynchy", "channels": {"general": {}}}},
        )
    )
    ch.workspaces = lambda: {"discord:channel:99": MagicMock()}
    interaction = _interaction()
    interaction.channel.id = "99"
    interaction.channel.name = "dynamic-workspace-thread"
    interaction.guild = MagicMock()
    interaction.guild.id = "1"
    interaction.guild.name = "pynchy"

    assert ch.is_interaction_allowed(interaction)


@pytest.mark.asyncio
async def test_send_approval_event_posts_controls_and_routes_decision():
    decision_callback = MagicMock()
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV, dm_policy="open"
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _msg: None,
        on_chat_metadata=lambda _jid, _ts, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        on_approval_decision=decision_callback,
    )
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_event(
        "discord:direct:42",
        OutboundEvent(
            type=OutboundEventType.APPROVAL,
            content="Approval required\n\n→ approve js / deny js",
            metadata={"short_id": "js"},
        ),
    )

    view = fake.sends[0][1]["view"]
    assert [item.label for item in view.children] == ["Approve", "Deny"]

    interaction = MagicMock()
    interaction.user.id = "42"
    interaction.user.bot = False
    interaction.user.roles = []
    interaction.channel.id = "42"
    interaction.channel.parent = None
    interaction.channel.parent_id = None
    interaction.channel.name = None
    interaction.guild = None
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()

    approve = view.children[0]
    await approve.callback(interaction)

    decision_callback.assert_called_once_with("discord:direct:42", "approve", "js", "42")
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_capability_approval_event_posts_duration_controls():
    decision_callback = MagicMock()
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV, dm_policy="open"
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _msg: None,
        on_chat_metadata=lambda _jid, _ts, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        on_approval_decision=decision_callback,
    )
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_event(
        "discord:direct:42",
        OutboundEvent(
            type=OutboundEventType.APPROVAL,
            content="Approval required",
            metadata={"short_id": "a8", "allow_remember": True},
        ),
    )

    view = fake.sends[0][1]["view"]
    assert [item.label for item in view.children] == [
        "Approve once",
        "Approve this session",
        "Approve forever",
        "Deny",
    ]
    await view.children[1].callback(_interaction())
    decision_callback.assert_called_once_with("discord:direct:42", "approve-session", "a8", "42")


@pytest.mark.asyncio
async def test_approval_button_rejects_an_unattached_view():
    _channel_instance, view = await _approval_view(callback=MagicMock())
    button = view.children[0]
    view.remove_item(button)

    with pytest.raises(RuntimeError, match="before the view was attached"):
        await button.callback(_interaction())


@pytest.mark.asyncio
async def test_empty_approval_event_does_not_send_an_empty_message():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_event(
        "discord:channel:1",
        OutboundEvent(
            type=OutboundEventType.APPROVAL,
            content="",
            metadata={"short_id": "js"},
        ),
    )

    assert fake.sends == []


@pytest.mark.asyncio
async def test_approval_controls_reject_unauthorized_and_duplicate_decisions():
    callback = MagicMock()
    ch, view = await _approval_view(callback=callback)
    interaction = _interaction()
    ch.is_interaction_allowed = lambda _interaction: False  # type: ignore[method-assign]

    await view.children[0].callback(interaction)

    callback.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to decide this approval.", ephemeral=True
    )

    ch.is_interaction_allowed = lambda _interaction: True  # type: ignore[method-assign]
    await view.children[0].callback(interaction)
    await view.children[1].callback(interaction)

    callback.assert_called_once_with("discord:direct:42", "approve", "js", "42")
    assert interaction.response.send_message.await_args_list[-1].args[0] == (
        "This approval has already been decided."
    )


@pytest.mark.asyncio
async def test_approval_controls_explain_when_no_decision_callback_is_available():
    _ch, view = await _approval_view(callback=None)
    interaction = _interaction()

    await view.children[0].callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "Approval controls are unavailable; use the command in the prompt instead.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_approval_timeout_does_not_edit_after_decision():
    ch, view = await _approval_view(callback=MagicMock())
    interaction = _interaction()

    await view.children[0].callback(interaction)
    ch.resolve_channel.reset_mock()  # type: ignore[union-attr]

    await view.on_timeout()

    ch.resolve_channel.assert_not_awaited()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_approval_timeout_disables_controls_and_marks_the_original_message():
    ch, view = await _approval_view(callback=MagicMock())
    message = MagicMock()
    message.edit = AsyncMock()
    destination = MagicMock()
    destination.fetch_message = AsyncMock(return_value=message)
    ch.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]
    view.bind_message_id("101")

    await view.on_timeout()

    assert all(item.disabled for item in view.children)
    destination.fetch_message.assert_awaited_once_with(101)
    assert "This approval expired." in message.edit.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_approval_timeout_ignores_discord_fetch_failure():
    ch, view = await _approval_view(callback=MagicMock())
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.DiscordException("offline")
    )
    view.bind_message_id("101")

    await view.on_timeout()

    assert all(item.disabled for item in view.children)


@pytest.mark.asyncio
async def test_send_reaction_ignores_non_discord_message_id():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for a non-Discord message id")
    )
    # slack-style id must be a no-op, not an error
    await ch.send_reaction("discord:channel:1", "slack-123", "u1", "👀")


@pytest.mark.asyncio
async def test_send_reaction_adds_emoji_to_discord_message():
    ch = _channel()
    ch.client = object()
    message = MagicMock()
    message.add_reaction = AsyncMock()
    destination = MagicMock()
    destination.fetch_message = AsyncMock(return_value=message)
    ch.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await ch.send_reaction("discord:channel:1", "discord-101", "u1", "👀")

    destination.fetch_message.assert_awaited_once_with(101)
    message.add_reaction.assert_awaited_once_with("👀")


@pytest.mark.asyncio
@pytest.mark.parametrize("jid", ["discord:channel:1", "discord:voice:2"])
async def test_send_reaction_skips_unavailable_or_voice_destination(jid: str):
    ch = _channel()
    ch.client = None if jid.endswith("channel:1") else object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve an unavailable destination")
    )

    await ch.send_reaction(jid, "discord-101", "u1", "👀")


@pytest.mark.asyncio
async def test_resolve_channel_caches_direct_message_channels():
    ch = _channel()

    user = _FakeUser()
    fetch_user = AsyncMock(return_value=user)
    ch.client = _DirectMessageClient(
        get_user=lambda _snowflake: None,
        fetch_user=fetch_user,
    )

    first = await ch.resolve_channel("discord:direct:42")
    second = await ch.resolve_channel("discord:direct:42")

    assert first is second
    assert fetch_user.await_count == 1
    assert user.create_dm_calls == 1


@pytest.mark.asyncio
async def test_disconnect_clears_direct_message_cache():
    ch = _channel()
    user = _FakeUser()
    fetch_user = AsyncMock(return_value=user)
    ch.client = _DirectMessageClient(
        get_user=lambda _snowflake: None,
        fetch_user=fetch_user,
    )
    ch.lifecycle.disconnect = AsyncMock()

    await ch.resolve_channel("discord:direct:42")
    await ch.disconnect()
    await ch.resolve_channel("discord:direct:42")

    assert fetch_user.await_count == 2


@pytest.mark.asyncio
async def test_set_typing_starts_background_refresh_and_stops_cleanly():
    ch = _channel()
    ch.client = object()
    fake = _FakeTypingChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.set_typing("discord:channel:1", is_typing=True)
    await asyncio.sleep(0)

    assert fake.typing_calls >= 1

    await ch.set_typing("discord:channel:1", is_typing=False)
    calls_after_stop = fake.typing_calls
    await asyncio.sleep(0)

    assert fake.typing_calls == calls_after_stop


@pytest.mark.asyncio
async def test_typing_loop_refreshes_until_cancelled(monkeypatch: pytest.MonkeyPatch):
    ch = _channel()
    ch.client = object()
    fake = _FakeTypingChannel()
    sleep_calls = 0
    orig_sleep = asyncio.sleep

    async def _fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        await orig_sleep(0)
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    await ch.set_typing("discord:channel:1", is_typing=True)
    for _ in range(3):
        await orig_sleep(0)

    assert fake.typing_calls == 2


@pytest.mark.asyncio
async def test_fetch_inbound_since_filters_bot_and_self():
    ch = _channel()
    ch.client = object()
    ch.bot_user_id = "self"

    def _msg(mid: str, author_id: str, *, bot: bool) -> _HistoryMessage:
        return _HistoryMessage(
            id=mid,
            author=_HistoryAuthor(id=author_id, bot=bot, display_name=f"user{author_id}"),
            channel=_HistoryChannel(id="1"),
            content=f"msg {mid}",
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )

    class _HistChannel:
        def history(self, **kwargs):
            async def gen():
                await asyncio.sleep(0)
                yield _msg("1", "human", bot=False)
                yield _msg("2", "otherbot", bot=True)
                yield _msg("3", "self", bot=False)

            return gen()

    ch.resolve_channel = AsyncMock(return_value=_HistChannel())  # type: ignore[method-assign]

    result = await ch.fetch_inbound_since("discord:channel:1", "2026-07-06T00:00:00+00:00")
    ids = [m.id for m in result.messages]
    assert ids == ["discord-1"]  # bot + own filtered out
    assert result.high_water_mark


@pytest.mark.asyncio
async def test_fetch_inbound_since_skips_forum_root_without_message_history():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]

    result = await ch.fetch_inbound_since("discord:channel:1", "2026-07-06T00:00:00+00:00")

    assert result.messages == []


@pytest.mark.parametrize("jid", ["discord:channel:1", "discord:voice:2"])
async def test_fetch_inbound_since_skips_unavailable_or_voice_destination(jid: str):
    ch = _channel()
    ch.client = None if jid.endswith("channel:1") else object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve an unavailable destination")
    )

    result = await ch.fetch_inbound_since(jid, "2026-07-06T00:00:00+00:00")

    assert result.messages == []


@pytest.mark.asyncio
async def test_send_ask_user_skips_voice_destination():
    ch = _channel()

    assert await ch.send_ask_user("discord:voice:2", "request-1", []) is None


def test_plugin_returns_none_without_context():
    assert DiscordChannelPlugin().pynchy_create_channel(context=None) is None


@pytest.mark.asyncio
async def test_post_event_sends_preview_and_returns_message_id():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    msg_id = await ch.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi there")
    )
    assert msg_id == "discord-101"
    assert fake.sends[0][0] == "hi there"
    # streamed previews must also use safe mention defaults
    assert fake.sends[0][1]["allowed_mentions"] is not None


@pytest.mark.asyncio
async def test_post_event_returns_none_for_empty_text():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for empty text")
    )
    result = await ch.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="   ")
    )
    assert result is None


@pytest.mark.asyncio
async def test_post_event_returns_none_when_too_large_to_stream():
    # A message over the single-message limit can't be an editable preview;
    # returning None makes core route it through chunked send_event instead.
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve when text exceeds the limit")
    )
    result = await ch.post_event(
        "discord:channel:1",
        OutboundEvent(type=OutboundEventType.TEXT, content="x" * 2001),
    )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("jid", ["slack:C1", "discord:voice:2"])
async def test_post_event_skips_non_text_destinations(jid: str):
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve a non-text destination")
    )

    assert (
        await ch.post_event(jid, OutboundEvent(type=OutboundEventType.TEXT, content="preview"))
        is None
    )


@pytest.mark.asyncio
async def test_update_event_edits_message_in_place():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    msg = await fake.send("initial", allowed_mentions=None)
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.update_event(
        "discord:channel:1",
        f"discord-{msg.id}",
        OutboundEvent(type=OutboundEventType.TEXT, content="updated text"),
    )
    assert msg.edits[-1][0] == "updated text"


@pytest.mark.asyncio
async def test_update_result_uses_discord_identity_without_mutating_shared_event():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    msg = await fake.send("initial", allowed_mentions=None)
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="final reply",
        metadata={"prefix_assistant_name": True, "turn_id": "turn-1"},
    )

    await ch.update_event("discord:channel:1", f"discord-{msg.id}", event)

    assert msg.edits[-1][0] == "final reply"
    assert event.metadata == {"prefix_assistant_name": True, "turn_id": "turn-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("jid", "message_id"),
    [("slack:C1", "slack-101"), ("discord:voice:2", "discord-101"), ("discord:channel:1", "bad")],
)
async def test_update_event_skips_non_streaming_targets(jid: str, message_id: str):
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve an invalid streaming target")
    )

    await ch.update_event(
        jid,
        message_id,
        OutboundEvent(type=OutboundEventType.TEXT, content="updated text"),
    )


@pytest.mark.asyncio
async def test_update_event_raises_when_too_large_so_core_falls_back():
    # Discord can't edit a message beyond the limit; raising lets sender.py
    # fall back to chunked send_event.
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="exceeds 2000 chars"):
        await ch.update_event(
            "discord:channel:1",
            "discord-101",
            OutboundEvent(type=OutboundEventType.TEXT, content="x" * 2001),
        )


@pytest.mark.asyncio
async def test_send_reaction_ignores_discord_fetch_failure():
    ch = _channel()
    ch.client = object()
    destination = MagicMock()
    destination.fetch_message = AsyncMock(side_effect=discord.DiscordException("offline"))
    ch.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await ch.send_reaction("discord:channel:1", "discord-101", "u1", "👀")


@pytest.mark.asyncio
async def test_typing_refresh_failure_does_not_escape_channel_boundary():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(side_effect=discord.DiscordException("offline"))  # type: ignore[method-assign]

    await ch.set_typing("discord:channel:1", is_typing=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_duplicate_typing_request_does_not_start_another_refresh():
    ch = _channel()
    ch.client = object()
    destination = _FakeTypingChannel()
    ch.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await ch.set_typing("discord:channel:1", is_typing=True)
    await asyncio.sleep(0)
    await ch.set_typing("discord:channel:1", is_typing=True)

    assert ch.resolve_channel.await_count == 1
    await ch.set_typing("discord:channel:1", is_typing=False)


async def test_stopping_typing_without_an_active_lease_is_safe():
    ch = _channel()

    await ch.set_typing("discord:channel:1", is_typing=False)


def test_streaming_channel_satisfies_protocol_and_is_detected():
    ch = _channel()
    # core detects streaming targets via hasattr on both methods
    assert hasattr(ch, "post_event")
    assert hasattr(ch, "update_event")
