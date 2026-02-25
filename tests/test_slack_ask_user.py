"""Tests for SlackChannel.send_ask_user and block_actions interaction handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.chat.plugins.slack import SlackChannel, _jid

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CHANNEL_ID = "C12345"
JID = _jid(CHANNEL_ID)
REQUEST_ID = "req-abc-123"


def _make_channel(
    *,
    on_ask_user_answer: object | None = None,
    allowed_channel_id: str = CHANNEL_ID,
) -> SlackChannel:
    """Create a SlackChannel with mocked internals for testing."""
    ch = SlackChannel(
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
    # Stub the Slack app so we don't need a real Socket Mode connection
    ch._app = MagicMock()
    ch._app.client.chat_postMessage = AsyncMock(return_value={"ts": "1234567890.123456"})
    ch._app.client.chat_update = AsyncMock(return_value={"ok": True})
    # Mark the test channel as allowed
    ch._allowed_channel_ids.add(allowed_channel_id)
    return ch


def _questions_with_options() -> list[dict]:
    return [
        {
            "question": "Which framework should I use?",
            "options": [
                {"label": "React", "description": "Popular SPA framework"},
                {"label": "Vue", "description": "Progressive framework"},
            ],
        }
    ]


def _questions_no_options() -> list[dict]:
    return [
        {
            "question": "What is the project name?",
        }
    ]


def _multi_questions() -> list[dict]:
    return [
        {
            "question": "Which framework?",
            "options": [
                {"label": "React", "description": "Popular SPA framework"},
                {"label": "Vue", "description": "Progressive framework"},
            ],
        },
        {
            "question": "What is the project name?",
        },
    ]


# ---------------------------------------------------------------------------
# send_ask_user tests
# ---------------------------------------------------------------------------


class TestSendAskUser:
    @pytest.mark.asyncio
    async def test_builds_correct_blocks_with_options(self) -> None:
        """Verify Block Kit payload has section, actions, and input blocks."""
        ch = _make_channel()
        await ch.send_ask_user(JID, REQUEST_ID, _questions_with_options())

        ch._app.client.chat_postMessage.assert_called_once()
        call_kwargs = ch._app.client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]

        # Should have: header section, actions with buttons, divider, input block
        block_types = [b["type"] for b in blocks]
        assert "section" in block_types, "Expected a section block for the question"
        assert "actions" in block_types, "Expected an actions block for option buttons"
        assert "input" in block_types, "Expected an input block for free-form text"

        # Verify section contains question text
        section_block = next(b for b in blocks if b["type"] == "section")
        assert "Which framework" in section_block["text"]["text"]

        # Verify actions block has correct buttons
        actions_block = next(b for b in blocks if b["type"] == "actions")
        button_texts = [
            el["text"]["text"] for el in actions_block["elements"] if el["type"] == "button"
        ]
        assert "React" in button_texts
        assert "Vue" in button_texts

        # Verify block_id encodes request_id
        assert any(REQUEST_ID in b.get("block_id", "") for b in blocks), (
            "Expected request_id encoded in at least one block_id"
        )

    @pytest.mark.asyncio
    async def test_returns_message_ts(self) -> None:
        """send_ask_user should return the ts of the posted message."""
        ch = _make_channel()
        ts = await ch.send_ask_user(JID, REQUEST_ID, _questions_with_options())
        assert ts == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_with_no_options(self) -> None:
        """Question with no options: section + input + submit, no option buttons."""
        ch = _make_channel()
        await ch.send_ask_user(JID, REQUEST_ID, _questions_no_options())

        call_kwargs = ch._app.client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]

        block_types = [b["type"] for b in blocks]
        assert "section" in block_types, "Expected a section block for the question"
        assert "input" in block_types, "Expected an input block for free-form text"

        # No option-button actions block — the only actions block is the submit button
        actions_blocks = [b for b in blocks if b["type"] == "actions"]
        for ab in actions_blocks:
            buttons = [el for el in ab["elements"] if el["type"] == "button"]
            for btn in buttons:
                # All buttons should be submit buttons, not option buttons
                assert btn["action_id"].startswith("ask_user_submit_"), (
                    f"Unexpected option button: {btn['action_id']}"
                )

    @pytest.mark.asyncio
    async def test_multiple_questions(self) -> None:
        """Multiple questions should each produce their own section + actions/input blocks."""
        ch = _make_channel()
        await ch.send_ask_user(JID, REQUEST_ID, _multi_questions())

        call_kwargs = ch._app.client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]

        # Count sections — should be at least 2 (one per question)
        section_blocks = [b for b in blocks if b["type"] == "section"]
        assert len(section_blocks) >= 2

        # First question has options → actions block
        assert any(b["type"] == "actions" for b in blocks)

        # Second question has no options → input block only
        input_blocks = [b for b in blocks if b["type"] == "input"]
        assert len(input_blocks) >= 1

    @pytest.mark.asyncio
    async def test_returns_none_for_wrong_jid(self) -> None:
        """send_ask_user should return None if the JID is not owned."""
        ch = _make_channel()
        result = await ch.send_ask_user("slack:WRONG", REQUEST_ID, _questions_with_options())
        assert result is None
        ch._app.client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_app(self) -> None:
        """send_ask_user should return None if the app is not initialized."""
        ch = _make_channel()
        ch._app = None
        result = await ch.send_ask_user(JID, REQUEST_ID, _questions_with_options())
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_text_set(self) -> None:
        """The chat_postMessage call should include a text fallback."""
        ch = _make_channel()
        await ch.send_ask_user(JID, REQUEST_ID, _questions_with_options())

        call_kwargs = ch._app.client.chat_postMessage.call_args.kwargs
        assert "text" in call_kwargs
        assert len(call_kwargs["text"]) > 0

    @pytest.mark.asyncio
    async def test_button_action_ids_encode_request_and_label(self) -> None:
        """Button action_id should encode request_id and option label for routing."""
        ch = _make_channel()
        await ch.send_ask_user(JID, REQUEST_ID, _questions_with_options())

        call_kwargs = ch._app.client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        actions_block = next(b for b in blocks if b["type"] == "actions")

        for button in actions_block["elements"]:
            if button["type"] == "button":
                action_id = button["action_id"]
                # action_id should contain the request_id and the label
                assert REQUEST_ID in action_id, (
                    f"Expected {REQUEST_ID!r} in action_id {action_id!r}"
                )


# ---------------------------------------------------------------------------
# block_actions interaction handler tests
# ---------------------------------------------------------------------------


class TestBlockActionHandlers:
    @pytest.mark.asyncio
    async def test_button_click_calls_callback(self) -> None:
        """Simulated button click should invoke on_ask_user_answer callback."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)

        # Register handlers (normally done in connect())
        ch._register_handlers()

        # Find the registered action handler
        # slack-bolt's @app.action() stores handlers internally.
        # We need to capture the handler function that was registered.
        action_handler = _extract_action_handler(ch._app)
        assert action_handler is not None, "Expected an action handler to be registered"

        # Simulate a Slack block_actions payload for a button click
        ack = AsyncMock()
        body = {
            "actions": [
                {
                    "action_id": f"ask_user_btn_{REQUEST_ID}_0_React",
                    "block_id": f"ask_user_actions_{REQUEST_ID}_0",
                    "type": "button",
                    "value": "React",
                }
            ],
            "channel": {"id": CHANNEL_ID},
            "message": {"ts": "1234567890.123456"},
            "user": {"id": "U999"},
        }

        await action_handler(ack=ack, body=body, action=body["actions"][0])
        ack.assert_called_once()
        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == REQUEST_ID
        answer_dict = call_args[0][1]
        assert answer_dict["answer"] == "React"
        assert answer_dict["answered_by"] == "U999"

    @pytest.mark.asyncio
    async def test_button_click_updates_message(self) -> None:
        """After a button click, the original message should be updated."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)
        ch._register_handlers()

        action_handler = _extract_action_handler(ch._app)

        ack = AsyncMock()
        body = {
            "actions": [
                {
                    "action_id": f"ask_user_btn_{REQUEST_ID}_0_React",
                    "block_id": f"ask_user_actions_{REQUEST_ID}_0",
                    "type": "button",
                    "value": "React",
                }
            ],
            "channel": {"id": CHANNEL_ID},
            "message": {"ts": "1234567890.123456"},
            "user": {"id": "U999"},
        }

        await action_handler(ack=ack, body=body, action=body["actions"][0])

        ch._app.client.chat_update.assert_called_once()
        update_kwargs = ch._app.client.chat_update.call_args.kwargs
        assert update_kwargs["channel"] == CHANNEL_ID
        assert update_kwargs["ts"] == "1234567890.123456"
        # Updated message should indicate the answer
        assert "React" in update_kwargs["text"]

    @pytest.mark.asyncio
    async def test_text_submit_calls_callback(self) -> None:
        """Simulated text input submission should invoke on_ask_user_answer callback."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)
        ch._register_handlers()

        action_handler = _extract_action_handler(ch._app)
        assert action_handler is not None

        # Simulate a text input submission action
        ack = AsyncMock()
        body = {
            "actions": [
                {
                    "action_id": f"ask_user_submit_{REQUEST_ID}",
                    "block_id": f"ask_user_input_{REQUEST_ID}_0",
                    "type": "button",
                    "value": "submit",
                }
            ],
            "channel": {"id": CHANNEL_ID},
            "message": {
                "ts": "1234567890.123456",
                "blocks": [
                    {
                        "type": "input",
                        "block_id": f"ask_user_input_{REQUEST_ID}_0",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": f"ask_user_text_{REQUEST_ID}_0",
                        },
                    }
                ],
            },
            "user": {"id": "U999"},
            "state": {
                "values": {
                    f"ask_user_input_{REQUEST_ID}_0": {
                        f"ask_user_text_{REQUEST_ID}_0": {
                            "type": "plain_text_input",
                            "value": "My custom answer",
                        }
                    }
                }
            },
        }

        await action_handler(ack=ack, body=body, action=body["actions"][0])
        ack.assert_called_once()
        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == REQUEST_ID
        answer_dict = call_args[0][1]
        assert answer_dict["answer"] == "My custom answer"

    @pytest.mark.asyncio
    async def test_no_callback_no_error(self) -> None:
        """If on_ask_user_answer is None, button clicks should not raise."""
        ch = _make_channel(on_ask_user_answer=None)
        ch._register_handlers()

        action_handler = _extract_action_handler(ch._app)

        ack = AsyncMock()
        body = {
            "actions": [
                {
                    "action_id": f"ask_user_btn_{REQUEST_ID}_0_React",
                    "block_id": f"ask_user_actions_{REQUEST_ID}_0",
                    "type": "button",
                    "value": "React",
                }
            ],
            "channel": {"id": CHANNEL_ID},
            "message": {"ts": "1234567890.123456"},
            "user": {"id": "U999"},
        }

        # Should not raise even without a callback
        await action_handler(ack=ack, body=body, action=body["actions"][0])
        ack.assert_called_once()


# ---------------------------------------------------------------------------
# on_ask_user_answer callback wiring tests
# ---------------------------------------------------------------------------


class TestOnAskUserAnswerCallback:
    def test_callback_stored_on_init(self) -> None:
        """on_ask_user_answer should be stored as an instance attribute."""
        cb = MagicMock()
        ch = _make_channel(on_ask_user_answer=cb)
        assert ch._on_ask_user_answer is cb

    def test_callback_defaults_to_none(self) -> None:
        """on_ask_user_answer should default to None."""
        ch = SlackChannel(
            connection_name="test",
            bot_token="xoxb-fake",
            app_token="xapp-fake",
            chat_names=[],
            allow_create=False,
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
        )
        assert ch._on_ask_user_answer is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_action_handler(mock_app: MagicMock) -> object | None:
    """Extract the handler function registered via ``@app.action(pattern)``.

    On a MagicMock, ``@app.action(pattern)`` is equivalent to::

        decorator = mock_app.action(pattern)   # returns mock_app.action.return_value
        decorator(handler_fn)                  # handler is the first positional arg

    All ``@app.action(...)`` calls share the same ``return_value`` mock, so
    the handler is always the first arg of the last call to that decorator.
    """
    decorator_mock = mock_app.action.return_value
    if decorator_mock.call_args_list:
        return decorator_mock.call_args_list[-1][0][0]
    return None
