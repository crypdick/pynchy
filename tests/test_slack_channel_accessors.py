"""Tests for SlackChannel public accessors used by interaction helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from pynchy.plugins.channels.slack import SlackChannel


def _fixture_token(prefix: str) -> str:
    return f"{prefix}-fixture"


def _make_channel(*, on_ask_user_answer: object | None = None) -> SlackChannel:
    return SlackChannel(
        connection_name="test-conn",
        bot_token=_fixture_token("xoxb"),
        app_token=_fixture_token("xapp"),
        chat_names=["general"],
        assistant_name="pynchy",
        allow_create=False,
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        on_reaction=None,
        on_ask_user_answer=on_ask_user_answer,
    )


def test_public_accessors_expose_internal_dependencies() -> None:
    """SlackChannel should expose the collaborators needed by interactions."""
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)

    assert ch.slack_app is None
    assert ch.on_ask_user_answer is callback
    assert ch.on_approval_decision is None
    assert ch.on_agent_stop is None


def test_public_state_accessors_round_trip_channel_configuration() -> None:
    ch = _make_channel()

    assert ch.connection_name == "test-conn"
    assert ch.bot_token == _fixture_token("xoxb")
    assert ch.app_token == _fixture_token("xapp")
    assert ch.handler is None
    assert ch.handler_task is None
    assert ch.reconnect_task is None
    assert not ch.bot_user_id
    assert ch.connected is False
    assert ch.shutting_down is False
    assert ch.allow_create is False
    assert ch.configured_chat_names == {"general"}

    handler = object()
    ch.handler = handler
    ch.handler_task = None
    ch.reconnect_task = None
    ch.bot_user_id = "U123"
    ch.connected = True
    ch.shutting_down = True

    assert ch.handler is handler
    assert ch.bot_user_id == "U123"
    assert ch.connected is True
    assert ch.shutting_down is True
