"""Tests for the built-in Slack channel plugin."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.channels import SlackConnectionSettings
from pynchy.plugins.api import ChannelPluginContext
from pynchy.plugins.channels.slack import (
    SlackChannel,
    SlackChannelPlugin,
    channel_id_from_jid,
    jid,
    split_text,
)
from tests.slack_test_support import (
    SLACK_APP_ENV,
    SLACK_BOT_ENV,
)
from tests.slack_test_support import (
    attach_slack_app as _attach_slack_app,
)
from tests.slack_test_support import (
    make_slack_channel as _make_channel,
)


def _plugin_context(
    slack_connections: dict[str, SlackConnectionSettings] | None = None,
) -> ChannelPluginContext:
    return ChannelPluginContext(
        on_message_callback=MagicMock(),
        on_chat_metadata_callback=MagicMock(),
        workspaces=MagicMock(return_value={}),
        send_message=MagicMock(),
        slack_connections=slack_connections or {},
    )


_HISTORY_SINCE = datetime.fromtimestamp(1_700_000_000, tz=UTC).isoformat()


# ------------------------------------------------------------------
# JID helpers
# ------------------------------------------------------------------


class TestJidHelpers:
    def test_jid_prefixes_channel_id(self) -> None:
        assert jid("C12345") == "slack:C12345"

    def test_channel_id_from_jid_strips_prefix(self) -> None:
        assert channel_id_from_jid("slack:C12345") == "C12345"

    def test_roundtrip(self) -> None:
        assert channel_id_from_jid(jid("C999")) == "C999"


# ------------------------------------------------------------------
# _split_text
# ------------------------------------------------------------------


class TestSplitText:
    def test_short_text_returns_single_chunk(self) -> None:
        assert split_text("hello", max_len=100) == ["hello"]

    def test_exact_boundary(self) -> None:
        text = "a" * 100
        assert split_text(text, max_len=100) == [text]

    def test_splits_on_newline(self) -> None:
        text = "a" * 50 + "\n" + "b" * 50
        chunks = split_text(text, max_len=60)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 50
        assert chunks[1] == "b" * 50

    def test_hard_split_when_no_newline(self) -> None:
        text = "a" * 200
        chunks = split_text(text, max_len=100)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 100
        assert chunks[1] == "a" * 100


# ------------------------------------------------------------------
# SlackChannel — unit tests (no real Slack connection)
# ------------------------------------------------------------------


class TestSlackChannelProtocol:
    def test_name_is_slack(self) -> None:
        ch = _make_channel()
        assert ch.name == "connection.slack.main"

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


class TestSlackChannelDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_sets_connected_false(self) -> None:
        ch = _make_channel()
        ch.connected = True
        ch.handler = MagicMock()
        ch.handler.close_async = AsyncMock()
        ch.handler_task = None

        await ch.disconnect()

        assert ch.connected is False


class TestReconnectShutdownRace:
    """Regression tests for the Socket Mode transport-exit boundary.

    If disconnect() is called while the reconnect backoff is sleeping, the
    transport-exit handler must bail out instead of calling connect()
    (which spawns aiohttp tasks that disconnect() can't cancel).
    """

    @pytest.mark.asyncio
    async def test_transport_exit_does_not_double_connect(self, monkeypatch) -> None:
        """If another path reconnected while we slept, don't double-connect."""
        ch = _make_channel()
        ch.connected = True
        ch.connect = AsyncMock()
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("websocket dropped")
        task.get_loop.return_value = asyncio.get_running_loop()
        monkeypatch.setattr(
            "pynchy.plugins.channels.slack._lifecycle.RECONNECT_INITIAL_DELAY_SECONDS",
            0.0,
        )

        ch.handle_socket_mode_exit(task)
        ch.connected = True
        reconnect_task = ch.reconnect_task
        assert reconnect_task is not None
        await reconnect_task

        ch.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_exit_reconnects(self, monkeypatch) -> None:
        """An unexpected Socket Mode exit reconnects the public channel."""
        ch = _make_channel()
        ch.connected = True

        async def fake_connect() -> None:
            await asyncio.sleep(0)
            ch.connected = True

        ch.connect = AsyncMock(side_effect=fake_connect)
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("websocket dropped")
        task.get_loop.return_value = asyncio.get_running_loop()
        monkeypatch.setattr(
            "pynchy.plugins.channels.slack._lifecycle.RECONNECT_INITIAL_DELAY_SECONDS",
            0.0,
        )

        ch.handle_socket_mode_exit(task)
        reconnect_task = ch.reconnect_task
        assert reconnect_task is not None
        await reconnect_task

        ch.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transport_exit_does_not_reconnect_after_shutdown(self) -> None:
        """prepare_shutdown() prevents reconnect even when _connected is False."""
        ch = _make_channel()
        ch.connected = True
        ch.connect = AsyncMock()
        ch.prepare_shutdown()
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("websocket dropped")

        ch.handle_socket_mode_exit(task)

        ch.connect.assert_not_awaited()
        task.get_loop.assert_not_called()

    def test_transport_exit_ignores_loop_shutdown_error(self) -> None:
        """create_task RuntimeError during loop shutdown doesn't propagate."""
        ch = _make_channel()
        ch.connected = True
        ch.shutting_down = False

        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("websocket dropped")
        task.get_loop.return_value.create_task.side_effect = RuntimeError(
            "Executor shutdown has been called"
        )

        # Should not raise
        ch.handle_socket_mode_exit(task)

        # _connected should be False (we tried to reconnect but couldn't)
        assert ch.connected is False


class TestPrepareShutdown:
    def test_sets_shutting_down_flag(self) -> None:
        ch = _make_channel()
        assert ch.shutting_down is False
        ch.prepare_shutdown()
        assert ch.shutting_down is True

    def test_channel_remains_connected(self) -> None:
        """prepare_shutdown doesn't disconnect — channel can still send messages."""
        ch = _make_channel()
        ch.connected = True
        ch.handler_task = MagicMock(spec=asyncio.Task)
        ch.handler_task.done.return_value = False

        ch.prepare_shutdown()

        assert ch.is_connected() is True
        assert ch.shutting_down is True


class TestSlackInboundBoundary:
    @pytest.mark.asyncio
    async def test_ingest_inbound_event_calls_callbacks(self) -> None:
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        _attach_slack_app(ch)

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "hello pynchy",
            "ts": "1234567890.000001",
            "channel_type": "channel",
        }
        await ch.ingest_inbound_event(event)

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
    @pytest.mark.parametrize(
        ("bot_user_id", "text", "expected"),
        [
            ("U_BOT", "<@U_BOT> hello", "@pynchy hello"),
            ("U_BOT", "hey <@U_BOT> hello", "hey @pynchy hello"),
            ("U_BOT", "<@U_OTHER> hello", "<@U_OTHER> hello"),
            ("", "<@U_BOT> hello", "<@U_BOT> hello"),
            ("U_BOT", "<@U_BOT> c", "@pynchy c"),
            ("U_BOT", "<@U_BOT>", "@pynchy"),
        ],
    )
    async def test_ingest_inbound_event_normalizes_bot_mentions(
        self,
        bot_user_id: str,
        text: str,
        expected: str,
    ) -> None:
        """Slack mentions become the downstream trigger through the public adapter."""
        on_message = MagicMock()
        ch = _make_channel(on_message=on_message)
        _attach_slack_app(ch)
        ch.bot_user_id = bot_user_id

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": text,
            "ts": "1234567890.000010",
            "channel_type": "channel",
        }
        await ch.ingest_inbound_event(event)

        msg = on_message.call_args[0][1]
        assert msg.content == expected

    @pytest.mark.asyncio
    async def test_ingest_inbound_event_deduplicates_one_slack_timestamp(self) -> None:
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        _attach_slack_app(ch)

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "hello",
            "ts": "1234567890.000020",
            "channel_type": "channel",
        }
        await ch.ingest_inbound_event(event)
        await ch.ingest_inbound_event(event)  # duplicate Slack app_mention

        on_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_inbound_event_ignores_bot_messages(self) -> None:
        on_message = MagicMock()
        ch = _make_channel(on_message=on_message)
        _attach_slack_app(ch)

        event = {"channel": "C12345", "user": "U999", "text": "bot msg", "bot_id": "B123"}
        await ch.ingest_inbound_event(event)

        on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_inbound_event_ignores_edits(self) -> None:
        on_message = MagicMock()
        ch = _make_channel(on_message=on_message)
        _attach_slack_app(ch)

        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "edited",
            "subtype": "message_changed",
        }
        await ch.ingest_inbound_event(event)

        on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_inbound_event_ignores_events_without_channel_or_user(self) -> None:
        on_message = MagicMock()
        ch = _make_channel(on_message=on_message)

        await ch.ingest_inbound_event({"channel": "C12345", "user": ""})

        on_message.assert_not_called()


# ------------------------------------------------------------------
# SlackChannelPlugin hook
# ------------------------------------------------------------------


class TestSlackChannelPlugin:
    def test_returns_none_without_plugin_context(self) -> None:
        assert SlackChannelPlugin().pynchy_create_channel(context=None) is None

    def test_returns_none_when_no_tokens(self) -> None:
        plugin = SlackChannelPlugin()
        context = _plugin_context()

        result = plugin.pynchy_create_channel(context=context)

        assert result is None

    def test_skips_connection_with_empty_token_environment_names(self) -> None:
        context = _plugin_context(
            {
                "main": SlackConnectionSettings(
                    bot_token_env="",
                    app_token_env="",
                    chat_names=("general",),
                    assistant_name="pynchy",
                    allow_create=False,
                )
            }
        )

        assert SlackChannelPlugin().pynchy_create_channel(context=context) is None

    def test_skips_connection_without_configured_chats(self) -> None:
        context = _plugin_context(
            {
                "main": SlackConnectionSettings(
                    bot_token_env=SLACK_BOT_ENV,
                    app_token_env=SLACK_APP_ENV,
                    chat_names=(),
                    assistant_name="pynchy",
                    allow_create=False,
                )
            }
        )

        assert SlackChannelPlugin().pynchy_create_channel(context=context) is None

    def test_skips_connection_with_missing_token_values(self) -> None:
        context = _plugin_context(
            {
                "main": SlackConnectionSettings(
                    bot_token_env=SLACK_BOT_ENV,
                    app_token_env=SLACK_APP_ENV,
                    chat_names=("general",),
                    assistant_name="pynchy",
                    allow_create=False,
                )
            }
        )

        with patch.dict(os.environ, {SLACK_BOT_ENV: "", SLACK_APP_ENV: ""}):
            assert SlackChannelPlugin().pynchy_create_channel(context=context) is None

    def test_returns_channel_when_tokens_present(self) -> None:
        plugin = SlackChannelPlugin()
        context = _plugin_context()

        context = _plugin_context(
            {
                "main": SlackConnectionSettings(
                    bot_token_env=SLACK_BOT_ENV,
                    app_token_env=SLACK_APP_ENV,
                    chat_names=("general",),
                    assistant_name="pynchy",
                    allow_create=True,
                )
            }
        )
        with patch.dict(os.environ, {"BOT": "xoxb-test", "APP": "xapp-test"}, clear=False):
            result = plugin.pynchy_create_channel(context=context)

        assert result is not None
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], SlackChannel)
        assert result[0].name == "main"


# ------------------------------------------------------------------
# History catch-up (public fetch_inbound_since)
# ------------------------------------------------------------------


class TestFetchInboundSince:
    @pytest.mark.asyncio
    async def test_returns_messages_in_chronological_order(self) -> None:
        ch = _make_channel()
        app = _attach_slack_app(ch)

        # Slack returns newest-first
        app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "second", "ts": "1700000002.000000"},
                    {"user": "U1", "text": "first", "ts": "1700000001.000000"},
                ]
            }
        )

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)
        messages = result.messages

        assert len(messages) == 2
        # Chronological order (oldest first)
        assert messages[0].content == "first"
        assert messages[1].content == "second"
        assert messages[0].id == "slack-1700000001.000000"
        assert messages[1].id == "slack-1700000002.000000"
        assert messages[0].chat_jid == "slack:C12345"

    @pytest.mark.asyncio
    async def test_filters_bot_messages(self) -> None:
        ch = _make_channel()
        app = _attach_slack_app(ch)

        app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "human", "ts": "1700000001.000000"},
                    {"user": "U2", "text": "bot", "ts": "1700000002.000000", "bot_id": "B1"},
                ]
            }
        )

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        assert len(result.messages) == 1
        assert result.messages[0].content == "human"

    @pytest.mark.asyncio
    async def test_filters_subtypes(self) -> None:
        ch = _make_channel()
        app = _attach_slack_app(ch)

        app.client.conversations_history = AsyncMock(
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

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        assert len(result.messages) == 1
        assert result.messages[0].content == "normal"

    @pytest.mark.asyncio
    async def test_normalizes_bot_mentions(self) -> None:
        ch = _make_channel()
        app = _attach_slack_app(ch)
        ch.bot_user_id = "U_BOT"

        app.client.conversations_history = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "<@U_BOT> c", "ts": "1700000001.000000"},
                ]
            }
        )

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        assert len(result.messages) == 1
        assert result.messages[0].content == "@pynchy c"

    @pytest.mark.asyncio
    async def test_handles_api_error(self) -> None:
        ch = _make_channel()
        app = _attach_slack_app(ch)
        app.client.conversations_history = AsyncMock(side_effect=Exception("API error"))

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        assert result.messages == []

    @pytest.mark.asyncio
    async def test_skips_bot_only_page_then_returns_next_user_message(self) -> None:
        ch = _make_channel()
        app = _attach_slack_app(ch)
        app.client.conversations_history = AsyncMock(
            side_effect=[
                {
                    "messages": [
                        {"user": "U2", "text": "bot", "ts": "1700000002.000000", "bot_id": "B1"},
                    ],
                    "has_more": True,
                },
                {
                    "messages": [
                        {"user": "U1", "text": "human", "ts": "1700000003.000000"},
                    ],
                    "has_more": False,
                },
            ]
        )

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        assert len(result.messages) == 1
        assert result.messages[0].content == "human"
        assert result.messages[0].id == "slack-1700000003.000000"
        assert result.high_water_mark.endswith("+00:00")
        assert app.client.conversations_history.await_count == 2

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_app(self) -> None:
        ch = _make_channel()
        ch.slack_app = None

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        assert result.messages == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_channel_outside_allowlist(self) -> None:
        ch = _make_channel()
        _attach_slack_app(ch)

        result = await ch.fetch_inbound_since("slack:C999", _HISTORY_SINCE)

        assert result.messages == []

    @pytest.mark.asyncio
    async def test_uses_actual_message_timestamp(self) -> None:
        """Timestamp should be derived from Slack ts, not current time."""
        ch = _make_channel()
        app = _attach_slack_app(ch)

        ts = "1700000001.000000"
        app.client.conversations_history = AsyncMock(
            return_value={"messages": [{"user": "U1", "text": "hi", "ts": ts}]}
        )

        result = await ch.fetch_inbound_since("slack:C12345", _HISTORY_SINCE)

        expected = datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
        assert result.messages[0].timestamp == expected


# ------------------------------------------------------------------
# Deterministic message IDs
# ------------------------------------------------------------------


class TestDeterministicMessageIds:
    @pytest.mark.asyncio
    async def test_ingest_inbound_event_uses_deterministic_id(self) -> None:
        on_message = MagicMock()
        on_metadata = MagicMock()
        ch = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        _attach_slack_app(ch)

        ts = "1234567890.000099"
        event = {
            "channel": "C12345",
            "user": "U999",
            "text": "hello",
            "ts": ts,
            "channel_type": "channel",
        }
        await ch.ingest_inbound_event(event)

        msg = on_message.call_args[0][1]
        assert msg.id == f"slack-{ts}"

    @pytest.mark.asyncio
    async def test_deterministic_id_is_stable_across_calls(self) -> None:
        """Same ts always produces the same message ID."""
        on_message = MagicMock()
        on_metadata = MagicMock()

        ts = "1234567890.000055"

        # First call through the public Slack event adapter.
        ch1 = _make_channel(on_message=on_message, on_chat_metadata=on_metadata)
        _attach_slack_app(ch1)
        await ch1.ingest_inbound_event(
            {"channel": "C1", "user": "U1", "text": "hi", "ts": ts, "channel_type": "channel"}
        )
        id_from_live = on_message.call_args[0][1].id

        # Second call via history catch-up
        ch2 = _make_channel()
        app = _attach_slack_app(ch2)
        app.client.conversations_history = AsyncMock(
            return_value={"messages": [{"user": "U1", "text": "hi", "ts": ts}]}
        )
        result = await ch2.fetch_inbound_since(
            "slack:C1", datetime.fromtimestamp(0, tz=UTC).isoformat()
        )
        id_from_catchup = result.messages[0].id

        assert id_from_live == id_from_catchup == f"slack-{ts}"
