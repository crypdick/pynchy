"""Tests for Slack interaction handlers using SlackChannel public accessors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pynchy.plugins.channels.slack._channel import SlackChannel
from pynchy.plugins.channels.slack._interactions import SlackInteractions


def _make_channel(*, on_ask_user_answer: object | None = None) -> SlackChannel:
    channel = SlackChannel(
        connection_name="test-conn",
        bot_token="xoxb-fake",
        app_token="xapp-fake",
        chat_names=["general"],
        allow_create=False,
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        on_reaction=None,
        on_ask_user_answer=on_ask_user_answer,
    )
    channel._app = MagicMock()
    channel._app.client.chat_update = AsyncMock(return_value={"ok": True})
    channel._allowed_channel_ids.add("C12345")
    return channel


def test_ask_user_interaction_uses_public_accessors() -> None:
    """ask_user submit handling should still call the callback and update Slack."""
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    interactions = SlackInteractions(ch)

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

    import asyncio

    asyncio.run(interactions._on_ask_user_interaction(body, body["actions"][0]))

    callback.assert_called_once()
    assert callback.call_args.args[0] == "req-abc-123"
    assert callback.call_args.args[1]["answer"] == "Use Svelte"
    ch._app.client.chat_update.assert_awaited_once()
