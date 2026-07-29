"""Tests for Slack interaction handlers using SlackChannel public accessors."""

from __future__ import annotations

import asyncio
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


def _make_channel(
    *,
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
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        on_reaction=None,
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
