"""Behavioral coverage for Slack configured-chat reconciliation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pynchy.plugins.channels.slack import SlackChannel
from tests.slack_test_support import (
    SLACK_APP_VALUE,
    SLACK_BOT_VALUE,
    attach_slack_app,
    make_slack_channel,
)


class TestSlackConfiguredChats:
    @pytest.mark.asyncio
    async def test_fetch_inbound_since_advances_past_bot_pages_and_preserves_newest_watermark(
        self,
    ) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_history.side_effect = [
            {"messages": [{"ts": "100", "bot_id": "B"}], "has_more": True},
            {"messages": [{"ts": "90", "user": "U1", "text": "hello"}], "has_more": False},
        ]

        result = await channel.fetch_inbound_since("slack:C12345", "1970-01-01T00:00:00+00:00")

        assert [message.content for message in result.messages] == ["hello"]
        assert result.high_water_mark == "1970-01-01T00:01:40+00:00"

    @pytest.mark.asyncio
    async def test_fetch_inbound_since_ignores_empty_and_malformed_pages(self) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_history.return_value = {
            "messages": [
                {"user": "U1", "text": "missing timestamp"},
                {"ts": "101", "text": "missing user"},
            ],
            "has_more": False,
        }

        result = await channel.fetch_inbound_since("slack:C12345", "1970-01-01T00:00:00+00:00")

        assert result.messages == []

        app.client.conversations_history.return_value = {"messages": [], "has_more": False}
        assert (
            await channel.fetch_inbound_since("slack:C12345", "1970-01-01T00:00:00+00:00")
        ).messages == []

    @pytest.mark.asyncio
    async def test_fetch_inbound_since_skips_unallowed_channel(self) -> None:
        channel = make_slack_channel()
        attach_slack_app(channel)

        result = await channel.fetch_inbound_since("slack:C99999", "1970-01-01T00:00:00+00:00")

        assert result.messages == []

    @pytest.mark.asyncio
    async def test_fetch_inbound_since_skips_when_slack_is_disconnected(self) -> None:
        channel = make_slack_channel()

        result = await channel.fetch_inbound_since("slack:C12345", "1970-01-01T00:00:00+00:00")

        assert result.messages == []

    @pytest.mark.asyncio
    async def test_sync_allowed_channels_revokes_access_when_config_has_no_chats(self) -> None:
        channel = SlackChannel(
            connection_name="connection.slack.main",
            bot_token=SLACK_BOT_VALUE,
            app_token=SLACK_APP_VALUE,
            chat_names=[],
            assistant_name="pynchy",
            allow_create=False,
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
        )
        channel.register_allowed_channel("legacy", "C111")

        await channel.sync_allowed_channels()

        assert channel.owns_jid("slack:C111") is False

    @pytest.mark.asyncio
    async def test_sync_allowed_channels_joins_and_registers_each_configured_chat(self) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_list.return_value = {
            "channels": [{"id": "C12345", "name": "general"}],
            "response_metadata": {"next_cursor": ""},
        }

        await channel.sync_allowed_channels()

        assert channel.owns_jid("slack:C12345") is True
        app.client.conversations_join.assert_awaited_once_with(channel="C12345")

    @pytest.mark.asyncio
    async def test_sync_allowed_channels_creates_a_missing_configured_chat(self) -> None:
        channel = SlackChannel(
            connection_name="connection.slack.main",
            bot_token=SLACK_BOT_VALUE,
            app_token=SLACK_APP_VALUE,
            chat_names=["release notes"],
            assistant_name="pynchy",
            allow_create=True,
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
        )
        app = attach_slack_app(channel)
        app.client.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }
        app.client.conversations_create.return_value = {"channel": {"id": "C222"}}

        await channel.sync_allowed_channels()

        assert channel.owns_jid("slack:C222") is True
        app.client.conversations_create.assert_awaited_once_with(
            name="release-notes", is_private=False
        )
        app.client.conversations_join.assert_awaited_once_with(channel="C222")

    @pytest.mark.asyncio
    async def test_sync_allowed_channels_does_not_grant_unknown_chat_without_creation(
        self,
    ) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }

        await channel.sync_allowed_channels()

        assert channel.owns_jid("slack:unknown") is False
        app.client.conversations_create.assert_not_awaited()
        app.client.conversations_join.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_chat_jid_finds_a_later_page_and_joins_the_channel(self) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_list.side_effect = [
            {"channels": [], "response_metadata": {"next_cursor": "next-page"}},
            {
                "channels": [{"id": "C987", "name": "engineering"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]

        resolved = await channel.resolve_chat_jid("Engineering")

        assert resolved == "slack:C987"
        assert channel.owns_jid(resolved) is True
        app.client.conversations_join.assert_awaited_once_with(channel="C987")
        assert app.client.conversations_list.await_args_list[1].kwargs["cursor"] == "next-page"

    @pytest.mark.asyncio
    async def test_resolve_chat_jid_uses_a_registered_chat_without_an_api_call(self) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)

        assert await channel.resolve_chat_jid("General") == "slack:C1"
        app.client.conversations_list.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_chat_jid_returns_none_when_creation_is_disabled(self) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }

        assert await channel.resolve_chat_jid("missing-chat") is None
        app.client.conversations_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_chat_jid_creates_an_unknown_chat_when_enabled(self) -> None:
        channel = SlackChannel(
            connection_name="connection.slack.main",
            bot_token=SLACK_BOT_VALUE,
            app_token=SLACK_APP_VALUE,
            chat_names=[],
            assistant_name="pynchy",
            allow_create=True,
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
        )
        app = attach_slack_app(channel)
        app.client.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }
        app.client.conversations_create.return_value = {"channel": {"id": "C444"}}

        assert await channel.resolve_chat_jid("Fresh Chat") == "slack:C444"
        assert channel.owns_jid("slack:C444") is True

    @pytest.mark.asyncio
    async def test_create_group_reuses_a_racing_channel_and_keeps_it_receivable(self) -> None:
        channel = SlackChannel(
            connection_name="connection.slack.main",
            bot_token=SLACK_BOT_VALUE,
            app_token=SLACK_APP_VALUE,
            chat_names=[],
            assistant_name="pynchy",
            allow_create=True,
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
        )
        app = attach_slack_app(channel)
        app.client.conversations_create.side_effect = RuntimeError("name_taken")
        app.client.conversations_list.return_value = {
            "channels": [{"id": "C432", "name": "release-notes"}],
            "response_metadata": {"next_cursor": ""},
        }

        created = await channel.create_group("Release Notes")

        assert created == "slack:C432"
        assert channel.owns_jid(created) is True
        assert "release-notes" in channel.configured_chat_names
        app.client.conversations_join.assert_awaited_once_with(channel="C432")

    @pytest.mark.asyncio
    async def test_create_group_rejects_a_malformed_provider_response(self) -> None:
        channel = make_slack_channel()
        app = attach_slack_app(channel)
        app.client.conversations_create.return_value = {"channel": {}}

        with pytest.raises(TypeError, match=r"missing channel\.id"):
            await channel.create_group("broken-response")
