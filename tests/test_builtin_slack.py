"""Tests for the built-in Slack channel plugin."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.chat.plugins.slack import (
    SlackChannel,
    SlackChannelPlugin,
    _channel_id_from_jid,
    _jid,
    _split_text,
)

# ------------------------------------------------------------------
# JID helpers
# ------------------------------------------------------------------


class TestJidHelpers:
    def test_jid_prefixes_channel_id(self) -> None:
        assert _jid("C12345") == "slack:C12345"

    def test_channel_id_from_jid_strips_prefix(self) -> None:
        assert _channel_id_from_jid("slack:C12345") == "C12345"

    def test_roundtrip(self) -> None:
        assert _channel_id_from_jid(_jid("C999")) == "C999"


# ------------------------------------------------------------------
# _split_text
# ------------------------------------------------------------------


class TestSplitText:
    def test_short_text_returns_single_chunk(self) -> None:
        assert _split_text("hello", max_len=100) == ["hello"]

    def test_exact_boundary(self) -> None:
        text = "a" * 100
        assert _split_text(text, max_len=100) == [text]

    def test_splits_on_newline(self) -> None:
        text = "a" * 50 + "\n" + "b" * 50
        chunks = _split_text(text, max_len=60)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 50
        assert chunks[1] == "b" * 50

    def test_hard_split_when_no_newline(self) -> None:
        text = "a" * 200
        chunks = _split_text(text, max_len=100)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 100
        assert chunks[1] == "a" * 100


# ------------------------------------------------------------------
# SlackChannel — unit tests (no real Slack connection)
# ------------------------------------------------------------------


def _make_channel(
    on_message: Any = None,
    on_chat_metadata: Any = None,
) -> SlackChannel:
    return SlackChannel(
        bot_token="xoxb-fake",
        app_token="xapp-fake",
        on_message=on_message or MagicMock(),
        on_chat_metadata=on_chat_metadata or MagicMock(),
    )


class TestSlackChannelProtocol:
    def test_name_is_slack(self) -> None:
        ch = _make_channel()
        assert ch.name == "slack"

    def test_prefix_assistant_name_is_false(self) -> None:
        ch = _make_channel()
        assert ch.prefix_assistant_name is False

    def test_is_connected_false_before_connect(self) -> None:
        ch = _make_channel()
        assert ch.is_connected() is False

    def test_owns_jid_positive(self) -> None:
        ch = _make_channel()
        assert ch.owns_jid("slack:C12345") is True

    def test_owns_jid_negative(self) -> None:
        ch = _make_channel()
        assert ch.owns_jid("whatsapp:12345@g.us") is False
        assert ch.owns_jid("C12345") is False


class TestSlackChannelSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_posts_to_correct_channel(self) -> None:
        ch = _make_channel()
        ch._connected = True
        ch._app = MagicMock()
        ch._app.client.chat_postMessage = AsyncMock()

        await ch.send_message("slack:C12345", "hello world")

        ch._app.client.chat_postMessage.assert_awaited_once_with(
            channel="C12345", text="hello world"
        )

    @pytest.mark.asyncio
    async def test_send_message_skips_non_owned_jid(self) -> None:
        ch = _make_channel()
        ch._connected = True
        ch._app = MagicMock()
        ch._app.client.chat_postMessage = AsyncMock()

        await ch.send_message("whatsapp:12345@g.us", "hello")

        ch._app.client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_message_splits_long_text(self) -> None:
        ch = _make_channel()
        ch._connected = True
        ch._app = MagicMock()
        ch._app.client.chat_postMessage = AsyncMock()

        long_text = "a" * 6000
        await ch.send_message("slack:C12345", long_text)

        assert ch._app.client.chat_postMessage.await_count == 2


class TestSlackChannelDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_sets_connected_false(self) -> None:
        ch = _make_channel()
        ch._connected = True
        ch._handler = MagicMock()
        ch._handler.close_async = AsyncMock()
        ch._handler_task = None

        await ch.disconnect()

        assert ch.is_connected() is False


class TestNormalizeBotMention:
    """_normalize_bot_mention replaces <@BOTID> with the canonical trigger."""

    def test_replaces_mention_at_start(self) -> None:
        ch = _make_channel()
        ch._bot_user_id = "U_BOT"
        result = ch._normalize_bot_mention("<@U_BOT> hello")
        assert result == "@pynchy hello"

    def test_replaces_mention_in_middle(self) -> None:
        ch = _make_channel()
        ch._bot_user_id = "U_BOT"
        result = ch._normalize_bot_mention("hey <@U_BOT> hello")
        assert result == "hey @pynchy hello"

    def test_preserves_other_mentions(self) -> None:
        ch = _make_channel()
        ch._bot_user_id = "U_BOT"
        assert ch._normalize_bot_mention("<@U_OTHER> hello") == "<@U_OTHER> hello"

    def test_noop_when_no_bot_id(self) -> None:
        ch = _make_channel()
        ch._bot_user_id = ""
        assert ch._normalize_bot_mention("<@U_BOT> hello") == "<@U_BOT> hello"

    def test_single_word_command_after_mention(self) -> None:
        ch = _make_channel()
        ch._bot_user_id = "U_BOT"
        result = ch._normalize_bot_mention("<@U_BOT> c")
        assert result == "@pynchy c"

    def test_mention_only(self) -> None:
        ch = _make_channel()
        ch._bot_user_id = "U_BOT"
        result = ch._normalize_bot_mention("<@U_BOT>")
        assert result == "@pynchy"

    def test_trigger_pattern_matches_after_normalize(self) -> None:
        """Verify the canonical trigger survives the trigger pattern check."""
        import re

        ch = _make_channel()
        ch._bot_user_id = "U_BOT"
        result = ch._normalize_bot_mention("<@U_BOT> do something")
        pattern = re.compile(r"^@pynchy\b", re.IGNORECASE)
        assert pattern.search(result), f"Trigger pattern should match: {result!r}"


class TestDedupTs:
    def test_first_ts_returns_false(self) -> None:
        ch = _make_channel()
        assert ch._dedup_ts("1234567890.000001") is False

    def test_duplicate_ts_returns_true(self) -> None:
        ch = _make_channel()
        ch._dedup_ts("1234567890.000001")
        assert ch._dedup_ts("1234567890.000001") is True

    def test_different_ts_returns_false(self) -> None:
        ch = _make_channel()
        ch._dedup_ts("1234567890.000001")
        assert ch._dedup_ts("1234567890.000002") is False


class TestSlackChannelInbound:
    @pytest.mark.asyncio
    async def test_on_slack_message_calls_callback(self) -> None:
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        ch._app = MagicMock()
        # Stub user/channel name resolution
        ch._resolve_user_name = AsyncMock(return_value="Alice")
        ch._resolve_channel_name = AsyncMock(return_value="general")

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "hello pynchy",
            "ts": "1234567890.000001",
            "channel_type": "channel",
        }
        await ch._on_slack_message(event)

        on_metadata.assert_called_once()
        meta_args = on_metadata.call_args[0]
        assert meta_args[0] == "slack:C12345"  # jid
        # Second arg should be an ISO timestamp, not the channel name
        assert "T" in meta_args[1]  # ISO format contains 'T'
        assert meta_args[2] == "general"  # channel name as third arg
        on_message.assert_called_once()
        msg = on_message.call_args[0][1]
        assert msg.chat_jid == "slack:C12345"
        assert msg.sender == "U999"
        assert msg.sender_name == "Alice"
        assert msg.content == "hello pynchy"

    @pytest.mark.asyncio
    async def test_on_slack_message_normalizes_bot_mention(self) -> None:
        """Bot @mention is replaced with canonical trigger, not stripped."""
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        ch._app = MagicMock()
        ch._bot_user_id = "U_BOT"
        ch._resolve_user_name = AsyncMock(return_value="Alice")
        ch._resolve_channel_name = AsyncMock(return_value="general")

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "<@U_BOT> c",
            "ts": "1234567890.000010",
            "channel_type": "channel",
        }
        await ch._on_slack_message(event)

        msg = on_message.call_args[0][1]
        assert msg.content == "@pynchy c"

    @pytest.mark.asyncio
    async def test_on_slack_message_deduplicates_same_ts(self) -> None:
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        ch._app = MagicMock()
        ch._resolve_user_name = AsyncMock(return_value="Alice")
        ch._resolve_channel_name = AsyncMock(return_value="general")

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "hello",
            "ts": "1234567890.000020",
            "channel_type": "channel",
        }
        await ch._on_slack_message(event)
        await ch._on_slack_message(event)  # duplicate (app_mention)

        on_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_slack_message_ignores_bot_messages(self) -> None:
        on_message = MagicMock()
        ch = _make_channel(on_message=on_message)
        ch._app = MagicMock()

        event = {"channel": "C12345", "user": "U999", "text": "bot msg", "bot_id": "B123"}
        await ch._on_slack_message(event)

        on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_slack_message_ignores_edits(self) -> None:
        on_message = MagicMock()
        ch = _make_channel(on_message=on_message)
        ch._app = MagicMock()

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "edited",
            "subtype": "message_changed",
        }
        await ch._on_slack_message(event)

        on_message.assert_not_called()


# ------------------------------------------------------------------
# SlackChannelPlugin hook
# ------------------------------------------------------------------


class TestSlackChannelPlugin:
    def test_returns_none_when_no_tokens(self) -> None:
        plugin = SlackChannelPlugin()
        context = MagicMock()

        with patch("pynchy.chat.plugins.slack.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.slack.bot_token = None
            cfg.slack.app_token = None
            mock_settings.return_value = cfg

            result = plugin.pynchy_create_channel(context=context)

        assert result is None

    def test_returns_channel_when_tokens_present(self) -> None:
        plugin = SlackChannelPlugin()
        context = MagicMock()

        with patch("pynchy.chat.plugins.slack.get_settings") as mock_settings:
            cfg = MagicMock()
            cfg.slack.bot_token.get_secret_value.return_value = "xoxb-test"
            cfg.slack.app_token.get_secret_value.return_value = "xapp-test"
            mock_settings.return_value = cfg

            result = plugin.pynchy_create_channel(context=context)

        assert result is not None
        assert isinstance(result, SlackChannel)
        assert result.name == "slack"
