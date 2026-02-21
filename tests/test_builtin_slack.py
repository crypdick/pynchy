"""Tests for the built-in Slack channel plugin."""

from __future__ import annotations

import asyncio
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


class TestReconnectShutdownRace:
    """Regression tests for the shutdown race in _reconnect_with_backoff.

    If disconnect() is called while the reconnect backoff is sleeping,
    _reconnect_with_backoff must bail out instead of calling connect()
    (which spawns aiohttp tasks that disconnect() can't cancel).
    """

    @pytest.mark.asyncio
    async def test_reconnect_aborts_when_already_connected(self) -> None:
        """If another path reconnected while we slept, don't double-connect."""
        ch = _make_channel()
        # Simulate: _on_handler_done set _connected=False and scheduled us,
        # but another path (e.g. forced reconnect()) already reconnected.
        ch._connected = True

        # Patch connect to detect if it gets called
        ch.connect = AsyncMock()

        await ch._reconnect_with_backoff(delay=0)

        ch.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconnect_proceeds_when_not_connected(self) -> None:
        """Normal reconnect path: _connected is False, so connect() runs."""
        ch = _make_channel()
        ch._connected = False

        # connect() will set _connected = True; mock it to avoid real Slack calls
        async def fake_connect() -> None:
            ch._connected = True

        ch.connect = AsyncMock(side_effect=fake_connect)

        await ch._reconnect_with_backoff(delay=0)

        ch.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconnect_aborts_when_shutting_down(self) -> None:
        """prepare_shutdown() prevents reconnect even when _connected is False."""
        ch = _make_channel()
        ch._connected = False
        ch._shutting_down = True

        ch.connect = AsyncMock()

        await ch._reconnect_with_backoff(delay=0)

        ch.connect.assert_not_awaited()

    def test_on_handler_done_skips_reconnect_when_shutting_down(self) -> None:
        """_on_handler_done does nothing after prepare_shutdown()."""
        ch = _make_channel()
        ch._connected = True
        ch._shutting_down = True

        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = None

        ch._on_handler_done(task)

        # Should not schedule a reconnect
        task.get_loop.assert_not_called()
        assert ch._reconnect_task is None

    def test_on_handler_done_catches_runtime_error(self) -> None:
        """create_task RuntimeError during loop shutdown doesn't propagate."""
        ch = _make_channel()
        ch._connected = True
        ch._shutting_down = False

        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("websocket dropped")
        task.get_loop.return_value.create_task.side_effect = RuntimeError(
            "Executor shutdown has been called"
        )

        # Should not raise
        ch._on_handler_done(task)

        # _connected should be False (we tried to reconnect but couldn't)
        assert ch._connected is False


class TestPrepareShutdown:
    def test_sets_shutting_down_flag(self) -> None:
        ch = _make_channel()
        assert ch._shutting_down is False
        ch.prepare_shutdown()
        assert ch._shutting_down is True

    def test_channel_remains_connected(self) -> None:
        """prepare_shutdown doesn't disconnect — channel can still send messages."""
        ch = _make_channel()
        ch._connected = True
        ch._handler_task = MagicMock(spec=asyncio.Task)
        ch._handler_task.done.return_value = False

        ch.prepare_shutdown()

        assert ch.is_connected() is True
        assert ch._shutting_down is True


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


# ------------------------------------------------------------------
# fetch_missed_messages (history catch-up)
# ------------------------------------------------------------------


class TestFetchMissedMessages:
    @pytest.mark.asyncio
    async def test_returns_messages_in_chronological_order(self) -> None:
        ch = _make_channel()
        ch._app = MagicMock()
        ch._resolve_user_name = AsyncMock(return_value="Alice")

        # Slack returns newest-first
        ch._app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "second", "ts": "1700000002.000000"},
                    {"user": "U1", "text": "first", "ts": "1700000001.000000"},
                ]
            }
        )

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        assert len(result) == 2
        # Chronological order (oldest first)
        assert result[0].content == "first"
        assert result[1].content == "second"
        assert result[0].id == "slack-1700000001.000000"
        assert result[1].id == "slack-1700000002.000000"
        assert result[0].chat_jid == "slack:C12345"

    @pytest.mark.asyncio
    async def test_filters_bot_messages(self) -> None:
        ch = _make_channel()
        ch._app = MagicMock()
        ch._resolve_user_name = AsyncMock(return_value="Alice")

        ch._app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "human", "ts": "1700000001.000000"},
                    {"user": "U2", "text": "bot", "ts": "1700000002.000000", "bot_id": "B1"},
                ]
            }
        )

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        assert len(result) == 1
        assert result[0].content == "human"

    @pytest.mark.asyncio
    async def test_filters_subtypes(self) -> None:
        ch = _make_channel()
        ch._app = MagicMock()
        ch._resolve_user_name = AsyncMock(return_value="Alice")

        ch._app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "normal", "ts": "1700000001.000000"},
                    {
                        "user": "U1",
                        "text": "edited",
                        "ts": "1700000002.000000",
                        "subtype": "message_changed",
                    },
                    {
                        "user": "U1",
                        "text": "joined",
                        "ts": "1700000003.000000",
                        "subtype": "channel_join",
                    },
                ]
            }
        )

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        assert len(result) == 1
        assert result[0].content == "normal"

    @pytest.mark.asyncio
    async def test_normalizes_bot_mentions(self) -> None:
        ch = _make_channel()
        ch._app = MagicMock()
        ch._bot_user_id = "U_BOT"
        ch._resolve_user_name = AsyncMock(return_value="Alice")

        ch._app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "<@U_BOT> c", "ts": "1700000001.000000"},
                ]
            }
        )

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        assert len(result) == 1
        assert result[0].content == "@pynchy c"

    @pytest.mark.asyncio
    async def test_handles_api_error(self) -> None:
        ch = _make_channel()
        ch._app = MagicMock()
        ch._app.client.conversations_history = AsyncMock(side_effect=Exception("API error"))

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_app(self) -> None:
        ch = _make_channel()
        ch._app = None

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        assert result == []

    @pytest.mark.asyncio
    async def test_uses_actual_message_timestamp(self) -> None:
        """Timestamp should be derived from Slack ts, not current time."""
        from datetime import UTC, datetime

        ch = _make_channel()
        ch._app = MagicMock()
        ch._resolve_user_name = AsyncMock(return_value="Alice")

        ts = "1700000001.000000"
        ch._app.client.conversations_history = AsyncMock(
            return_value={"messages": [{"user": "U1", "text": "hi", "ts": ts}]}
        )

        result = await ch.fetch_missed_messages("C12345", "1700000000.000000")

        expected = datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
        assert result[0].timestamp == expected


# ------------------------------------------------------------------
# Deterministic message IDs
# ------------------------------------------------------------------


class TestDeterministicMessageIds:
    @pytest.mark.asyncio
    async def test_on_slack_message_uses_deterministic_id(self) -> None:
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        ch._app = MagicMock()
        ch._resolve_user_name = AsyncMock(return_value="Alice")
        ch._resolve_channel_name = AsyncMock(return_value="general")

        ts = "1234567890.000099"
        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "hello",
            "ts": ts,
            "channel_type": "channel",
        }
        await ch._on_slack_message(event)

        msg = on_message.call_args[0][1]
        assert msg.id == f"slack-{ts}"

    @pytest.mark.asyncio
    async def test_deterministic_id_is_stable_across_calls(self) -> None:
        """Same ts always produces the same message ID."""
        on_message = MagicMock()
        on_metadata = MagicMock()

        ts = "1234567890.000055"

        # First call via _on_slack_message
        ch1 = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        ch1._app = MagicMock()
        ch1._resolve_user_name = AsyncMock(return_value="Alice")
        ch1._resolve_channel_name = AsyncMock(return_value="general")
        await ch1._on_slack_message(
            {"channel": "C1", "user": "U1", "text": "hi", "ts": ts, "channel_type": "channel"}
        )
        id_from_live = on_message.call_args[0][1].id

        # Second call via fetch_missed_messages
        ch2 = _make_channel()
        ch2._app = MagicMock()
        ch2._resolve_user_name = AsyncMock(return_value="Alice")
        ch2._app.client.conversations_history = AsyncMock(
            return_value={"messages": [{"user": "U1", "text": "hi", "ts": ts}]}
        )
        msgs = await ch2.fetch_missed_messages("C1", "0")
        id_from_catchup = msgs[0].id

        assert id_from_live == id_from_catchup == f"slack-{ts}"
