"""Tests for Slack interaction handlers using SlackChannel public accessors."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pynchy.plugins.channels.slack import SlackChannel

SLACK_BOT_VALUE = "xoxb-fake"
SLACK_APP_VALUE = "xapp-fake"


class _FakeSlackClient:
    def __init__(self) -> None:
        self.chat_update = AsyncMock(return_value={"ok": True})


class _FakeSlackApp:
    def __init__(self) -> None:
        self.client = _FakeSlackClient()


class _RegisteringSlackApp:
    def __init__(self) -> None:
        self.client = _FakeSlackClient()
        self.events: dict[str, Any] = {}
        self.actions: list[tuple[object, Any]] = []
        self.middleware: object | None = None

    def event(self, name: str):
        def register(handler: object) -> object:
            self.events[name] = handler
            return handler

        return register

    def action(self, pattern: object):
        def register(handler: object) -> object:
            self.actions.append((pattern, handler))
            return handler

        return register

    def use(self, middleware: object) -> None:
        self.middleware = middleware


def _make_channel(
    *,
    on_message: object | None = None,
    on_chat_metadata: object | None = None,
    on_reaction: object | None = None,
    on_ask_user_answer: object | None = None,
    on_approval_decision: object | None = None,
    on_agent_stop: object | None = None,
) -> SlackChannel:
    channel = SlackChannel(
        connection_name="test-conn",
        bot_token=SLACK_BOT_VALUE,
        app_token=SLACK_APP_VALUE,
        chat_names=["general"],
        assistant_name="pynchy",
        allow_create=False,
        on_message=on_message or MagicMock(),
        on_chat_metadata=on_chat_metadata or MagicMock(),
        on_reaction=on_reaction,
        on_ask_user_answer=on_ask_user_answer,
        on_approval_decision=on_approval_decision,
        on_agent_stop=on_agent_stop,
    )
    channel.slack_app = _FakeSlackApp()
    channel.register_allowed_channel("general", "C12345")
    return channel


def test_ask_user_interaction_uses_public_accessors() -> None:
    """ask_user submit handling should still call the callback and update Slack."""
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    interactions = ch.interactions

    body = {
        "actions": [
            {
                "action_id": "ask_user_submit_req-abc-123",
                "block_id": "ask_user_submit_actions_req-abc-123",
            }
        ],
        "channel": {"id": "C12345"},
        "message": {"ts": "1234567890.123456"},
        "user": {"id": "U999"},
        "state": {
            "values": {
                "ask_user_input_req-abc-123_0": {
                    "ask_user_text_req-abc-123_0": {
                        "type": "plain_text_input",
                        "value": "Use Svelte",
                    }
                }
            }
        },
    }

    asyncio.run(interactions._on_ask_user_interaction(body, body["actions"][0]))

    callback.assert_called_once()
    assert callback.call_args.args[0] == "req-abc-123"
    assert callback.call_args.args[1]["answer"] == "Use Svelte"
    ch.slack_app.client.chat_update.assert_awaited_once()


def test_approval_interaction_invokes_callback_and_removes_buttons() -> None:
    callback = MagicMock()
    ch = _make_channel(on_approval_decision=callback)
    body = {
        "channel": {"id": "C12345"},
        "message": {
            "ts": "123.456",
            "blocks": [{"type": "section"}, {"type": "actions", "elements": []}],
        },
        "user": {"id": "U999", "username": "Ada"},
    }

    asyncio.run(ch.interactions.on_approval_interaction(body, {"action_id": "cop_approve_a1"}))

    callback.assert_called_once_with("slack:C12345", "approve", "a1", "U999")
    update = ch.slack_app.client.chat_update.await_args.kwargs
    assert update["text"] == "Approved by Ada"
    assert all(block["type"] != "actions" for block in update["blocks"])
    assert update["blocks"][-1]["elements"][0]["text"] == "✅ Approved by <@U999>"


def test_stop_interaction_invokes_callback_and_removes_buttons() -> None:
    callback = MagicMock()
    ch = _make_channel(on_agent_stop=callback)
    body = {
        "channel": {"id": "C12345"},
        "message": {
            "ts": "123.456",
            "blocks": [{"type": "section"}, {"type": "actions", "elements": []}],
        },
        "user": {"id": "U999", "username": "Ada"},
    }

    asyncio.run(ch.interactions.on_agent_stop_interaction(body, {"action_id": "agent_stop_ops"}))

    callback.assert_called_once_with("ops", "U999")
    update = ch.slack_app.client.chat_update.await_args.kwargs
    assert update["text"] == "Stopped by Ada"
    assert all(block["type"] != "actions" for block in update["blocks"])
    assert update["blocks"][-1]["elements"][0]["text"] == "⏹ Stopped by <@U999>"


def test_stop_interaction_without_callback_still_finalizes_message() -> None:
    ch = _make_channel()
    body = {
        "channel": {"id": "C12345"},
        "message": {"ts": "123.456", "blocks": []},
        "user": {"id": "U999", "username": "Ada"},
    }

    asyncio.run(ch.interactions.on_agent_stop_interaction(body, {"action_id": "agent_stop_ops"}))

    ch.slack_app.client.chat_update.assert_awaited_once()


def test_ask_user_interaction_ignores_non_submit_actions() -> None:
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    body = {"channel": {"id": "C12345"}}

    asyncio.run(
        ch.interactions.on_ask_user_interaction(body, {"action_id": "ask_user_checkbox_a1"})
    )
    asyncio.run(ch.interactions.on_ask_user_interaction(body, {"action_id": "unrelated"}))

    callback.assert_not_called()
    ch.slack_app.client.chat_update.assert_not_awaited()


def test_interactions_ignore_disallowed_channels() -> None:
    ask_callback = MagicMock()
    approval_callback = MagicMock()
    stop_callback = MagicMock()
    ch = _make_channel(
        on_ask_user_answer=ask_callback,
        on_approval_decision=approval_callback,
        on_agent_stop=stop_callback,
    )
    body = {
        "channel": {"id": "C-other"},
        "message": {"ts": "123.456", "blocks": []},
        "user": {"id": "U999"},
    }

    async def scenario() -> None:
        await ch.interactions.on_ask_user_interaction(body, {"action_id": "ask_user_submit_req-1"})
        await ch.interactions.on_approval_interaction(body, {"action_id": "cop_approve_a1"})
        await ch.interactions.on_agent_stop_interaction(body, {"action_id": "agent_stop_ops"})

    asyncio.run(scenario())

    ask_callback.assert_not_called()
    approval_callback.assert_not_called()
    stop_callback.assert_not_called()
    ch.slack_app.client.chat_update.assert_not_awaited()


def test_ask_user_update_failure_does_not_hide_answer() -> None:
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.slack_app.client.chat_update.side_effect = RuntimeError("Slack unavailable")
    body = {
        "channel": {"id": "C12345"},
        "message": {"ts": "123.456"},
        "user": {"id": "U999"},
        "state": {"values": {}},
    }

    asyncio.run(
        ch.interactions.on_ask_user_interaction(body, {"action_id": "ask_user_submit_req-1"})
    )

    callback.assert_called_once_with(
        "req-1",
        {
            "answer": "",
            "answered_by": "U999",
            "channel_id": "C12345",
            "message_ts": "123.456",
        },
    )
    ch.slack_app.client.chat_update.assert_awaited_once()


def test_ask_user_submit_without_callback_or_source_message_is_safe() -> None:
    ch = _make_channel()
    body = {
        "channel": {"id": "C12345"},
        "message": {},
        "user": {"id": "U999"},
        "state": {"values": {}},
    }

    asyncio.run(
        ch.interactions.on_ask_user_interaction(body, {"action_id": "ask_user_submit_req-1"})
    )

    ch.slack_app.client.chat_update.assert_not_awaited()


def test_approval_interaction_ignores_malformed_action_id() -> None:
    callback = MagicMock()
    ch = _make_channel(on_approval_decision=callback)
    body = {"channel": {"id": "C12345"}, "message": {"ts": "123.456"}}

    asyncio.run(ch.interactions.on_approval_interaction(body, {"action_id": "cop_approve"}))

    callback.assert_not_called()
    ch.slack_app.client.chat_update.assert_not_awaited()


def test_decision_update_failure_does_not_hide_approval() -> None:
    callback = MagicMock()
    ch = _make_channel(on_approval_decision=callback)
    ch.slack_app.client.chat_update.side_effect = RuntimeError("Slack unavailable")
    body = {
        "channel": {"id": "C12345"},
        "message": {"ts": "123.456", "blocks": []},
        "user": {"id": "U999"},
    }

    asyncio.run(ch.interactions.on_approval_interaction(body, {"action_id": "cop_deny_a1"}))

    callback.assert_called_once_with("slack:C12345", "deny", "a1", "U999")
    ch.slack_app.client.chat_update.assert_awaited_once()


def test_agent_stop_interaction_ignores_blank_group_name() -> None:
    callback = MagicMock()
    ch = _make_channel(on_agent_stop=callback)
    body = {"channel": {"id": "C12345"}, "message": {"ts": "123.456"}}

    asyncio.run(ch.interactions.on_agent_stop_interaction(body, {"action_id": "agent_stop_"}))

    callback.assert_not_called()
    ch.slack_app.client.chat_update.assert_not_awaited()


def test_inbound_message_normalizes_mentions_and_deduplicates() -> None:
    on_message = MagicMock()
    on_chat_metadata = MagicMock()
    ch = _make_channel(on_message=on_message, on_chat_metadata=on_chat_metadata)
    ch.bot_user_id = "UBOT"
    ch.resolve_user_name = AsyncMock(return_value="Ada")
    ch.resolve_channel_name = AsyncMock(return_value="general")
    event = {
        "channel": "C12345",
        "user": "U999",
        "text": "<@UBOT> status",
        "ts": "123.456",
        "channel_type": "channel",
    }

    asyncio.run(ch.ingest_inbound_event(event))
    asyncio.run(ch.ingest_inbound_event(event))

    on_chat_metadata.assert_called_once()
    on_message.assert_called_once()
    jid, message = on_message.call_args.args
    assert jid == "slack:C12345"
    assert message.sender_name == "Ada"
    assert message.content == "@pynchy status"
    assert message.metadata == {"slack_ts": "123.456", "slack_channel_type": "channel"}


def test_inbound_message_ignores_bot_and_unallowed_channels() -> None:
    on_message = MagicMock()
    ch = _make_channel(on_message=on_message)

    asyncio.run(ch.ingest_inbound_event({"bot_id": "B1"}))
    asyncio.run(
        ch.ingest_inbound_event(
            {"channel": "C-other", "user": "U999", "text": "hello", "ts": "123.456"}
        )
    )

    on_message.assert_not_called()


def test_channel_disconnect_and_reconnect_manage_the_socket_handler() -> None:
    async def scenario() -> None:
        ch = _make_channel()
        handler = MagicMock()
        handler.close_async = AsyncMock()
        reconnect = AsyncMock()
        ch.handler = handler
        ch.connected = True
        ch.connect = reconnect

        await ch.disconnect()
        assert ch.connected is False
        handler.close_async.assert_awaited_once()

        ch.connected = True
        await ch.reconnect()
        assert ch.handler is None
        assert ch.handler_task is None
        reconnect.assert_awaited_once()

    asyncio.run(scenario())


def test_channel_connect_initializes_the_socket_mode_lifecycle(monkeypatch) -> None:
    class FakeClient:
        async def auth_test(self) -> dict[str, str]:
            return {"user_id": "UBOT"}

    class FakeApp:
        instances: list[FakeApp] = []

        def __init__(self, *, token: str) -> None:
            self.token = token
            self.client = FakeClient()
            self.instances.append(self)

    class FakeHandler:
        instances: list[FakeHandler] = []

        def __init__(self, app: FakeApp, app_token: str) -> None:
            self.app = app
            self.app_token = app_token
            self.close_async = AsyncMock()
            self.instances.append(self)

        async def start_async(self) -> None:
            await asyncio.Event().wait()

    bolt = ModuleType("slack_bolt")
    adapter = ModuleType("slack_bolt.adapter")
    socket_mode = ModuleType("slack_bolt.adapter.socket_mode")
    handler_module = ModuleType("slack_bolt.adapter.socket_mode.async_handler")
    app_module = ModuleType("slack_bolt.async_app")
    handler_module.AsyncSocketModeHandler = FakeHandler
    app_module.AsyncApp = FakeApp
    bolt.adapter = adapter
    adapter.socket_mode = socket_mode
    socket_mode.async_handler = handler_module  # noqa: V101
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", adapter)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", socket_mode)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode.async_handler", handler_module)
    monkeypatch.setitem(sys.modules, "slack_bolt.async_app", app_module)

    async def scenario() -> None:
        ch = _make_channel()
        sync_allowed_channels = AsyncMock()
        register_handlers = MagicMock()
        ch.sync_allowed_channels = sync_allowed_channels
        ch.register_inbound_handlers = register_handlers

        await ch.connect()

        assert ch.connected is True
        assert ch.bot_user_id == "UBOT"
        assert FakeApp.instances[0].token == SLACK_BOT_VALUE
        assert FakeHandler.instances[0].app_token == SLACK_APP_VALUE
        sync_allowed_channels.assert_awaited_once()
        register_handlers.assert_called_once()

        await ch.disconnect()
        FakeHandler.instances[0].close_async.assert_awaited_once()

    asyncio.run(scenario())


def test_socket_mode_exit_schedules_reconnect_for_an_active_channel() -> None:
    async def scenario() -> None:
        ch = _make_channel()
        ch.connected = True

        async def interrupted() -> None:
            await asyncio.sleep(0)
            raise RuntimeError("socket closed")

        task = asyncio.create_task(interrupted())
        with contextlib.suppress(RuntimeError):
            await task

        ch.handle_socket_mode_exit(task)

        assert ch.connected is False
        assert ch.reconnect_task is not None
        ch.reconnect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ch.reconnect_task

    asyncio.run(scenario())


def test_registered_slack_callbacks_ack_and_route_inbound_events(
    monkeypatch,
) -> None:
    on_message = MagicMock()
    on_reaction = MagicMock()
    on_ask_user_answer = MagicMock()
    on_agent_stop = MagicMock()
    ch = _make_channel(
        on_message=on_message,
        on_reaction=on_reaction,
        on_ask_user_answer=on_ask_user_answer,
        on_agent_stop=on_agent_stop,
    )
    ch.resolve_user_name = AsyncMock(return_value="Ada")
    ch.resolve_channel_name = AsyncMock(return_value="general")
    app = _RegisteringSlackApp()
    ch.slack_app = app
    ack = AsyncMock()
    monkeypatch.setattr(type(ch.events), "_register_assistant_handlers", lambda _events: None)

    ch.register_inbound_handlers()

    async def scenario() -> None:
        message_handler = app.events["message"]
        await message_handler(
            {"channel": "C12345", "user": "U999", "text": "hello", "ts": "123.456"},
            object(),
        )
        await app.events["app_mention"](
            {"channel": "C12345", "user": "U999", "text": "hello", "ts": "123.456"},
            object(),
        )
        reaction_handler = app.events["reaction_added"]
        await reaction_handler(
            {
                "user": "U999",
                "reaction": "eyes",
                "item": {"channel": "C12345", "ts": "123.456"},
            }
        )
        await reaction_handler({"reaction": "eyes"})
        await reaction_handler(
            {
                "user": "U999",
                "reaction": "eyes",
                "item": {"channel": "C-other", "ts": "123.456"},
            }
        )
        ask_user_handler = app.actions[0][1]
        await ask_user_handler(
            ack,
            {
                "channel": {"id": "C12345"},
                "message": {"ts": "123.456"},
                "user": {"id": "U999"},
            },
            {"action_id": "ask_user_submit_a1"},
        )
        approval_handler = app.actions[1][1]
        await approval_handler(
            ack,
            {"channel": {"id": "C12345"}, "user": {"id": "U999"}},
            {"action_id": "cop_approve_a1"},
        )
        stop_handler = app.actions[2][1]
        await stop_handler(
            ack,
            {"channel": {"id": "C12345"}, "user": {"id": "U999"}},
            {"action_id": "agent_stop_a1"},
        )

    asyncio.run(scenario())

    assert ack.await_count == 3
    on_message.assert_called_once()
    on_reaction.assert_called_once_with("slack:C12345", "123.456", "U999", "eyes")
    on_agent_stop.assert_called_once()


def test_registered_slack_assistant_callbacks_route_sidebar_messages(monkeypatch) -> None:
    class FakeAssistant:
        instances: list[FakeAssistant] = []

        def __init__(self) -> None:
            self.thread_started_handler: Any = None
            self.user_message_handler: Any = None
            self.instances.append(self)

        def thread_started(self, handler: Any) -> Any:
            self.thread_started_handler = handler
            return handler

        def user_message(self, handler: Any) -> Any:
            self.user_message_handler = handler
            return handler

    class AssistantContext:
        channel_id = "C12345"

    bolt = ModuleType("slack_bolt")
    middleware = ModuleType("slack_bolt.middleware")
    assistant = ModuleType("slack_bolt.middleware.assistant")
    assistant_module = ModuleType("slack_bolt.middleware.assistant.async_assistant")
    assistant_module.AsyncAssistant = FakeAssistant
    bolt.middleware = middleware
    middleware.assistant = assistant
    assistant.async_assistant = assistant_module  # noqa: V101
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.middleware", middleware)
    monkeypatch.setitem(sys.modules, "slack_bolt.middleware.assistant", assistant)
    monkeypatch.setitem(
        sys.modules,
        "slack_bolt.middleware.assistant.async_assistant",
        assistant_module,
    )

    on_message = MagicMock()
    on_chat_metadata = MagicMock()
    ch = _make_channel(on_message=on_message, on_chat_metadata=on_chat_metadata)
    ch.resolve_user_name = AsyncMock(return_value="Ada")
    app = _RegisteringSlackApp()
    ch.slack_app = app
    ch.register_inbound_handlers()
    panel = FakeAssistant.instances[0]
    say = AsyncMock()
    prompts = AsyncMock()
    status = AsyncMock()

    async def scenario() -> None:
        await panel.thread_started_handler(say, prompts)
        await panel.user_message_handler(
            {"user": "U999", "text": "status", "ts": "123.456"},
            AssistantContext(),
            status,
        )
        await panel.user_message_handler(
            {"text": "missing user", "ts": "123.457"},
            AssistantContext(),
            status,
        )

        class DisallowedContext:
            channel_id = "C-other"

        await panel.user_message_handler(
            {"user": "U999", "text": "not allowed", "ts": "123.458"},
            DisallowedContext(),
            status,
        )

    asyncio.run(scenario())

    say.assert_awaited_once_with("How can I help?")
    prompts.assert_awaited_once()
    assert status.await_count == 3
    assert all(call.args == ("thinking...",) for call in status.await_args_list)
    on_chat_metadata.assert_called_once()
    on_message.assert_called_once()
    assert on_message.call_args.args[1].metadata["slack_channel_type"] == "assistant"
