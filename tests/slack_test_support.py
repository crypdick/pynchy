"""Reusable SDK-shaped Slack fixtures for channel behavior tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pynchy.plugins.channels.slack import SlackChannel

SLACK_BOT_VALUE = "xoxb-fake"
SLACK_APP_VALUE = "xapp-fake"
SLACK_BOT_ENV = "BOT"
SLACK_APP_ENV = "APP"


class FakeSlackClient:
    def __init__(self) -> None:
        self.chat_postMessage = AsyncMock()
        self.chat_update = AsyncMock()
        self.reactions_add = AsyncMock()
        self.conversations_history = AsyncMock()
        self.users_info = AsyncMock()
        self.conversations_info = AsyncMock()
        self.conversations_join = AsyncMock()
        self.conversations_create = AsyncMock()
        self.conversations_list = AsyncMock()


class FakeSlackApp:
    def __init__(self) -> None:
        self.client = FakeSlackClient()


def attach_slack_app(channel: SlackChannel) -> FakeSlackApp:
    """Attach a fake app with observable identity lookups to a channel."""
    app = FakeSlackApp()
    app.client.users_info.return_value = {"user": {"profile": {"display_name": "Alice"}}}
    app.client.conversations_info.return_value = {"channel": {"name": "general"}}
    channel.slack_app = app
    return app


def make_slack_channel(
    on_message: Any = None,
    on_chat_metadata: Any = None,
) -> SlackChannel:
    """Build the standard configured Slack channel used by behavior tests."""
    channel = SlackChannel(
        connection_name="connection.slack.main",
        bot_token=SLACK_BOT_VALUE,
        app_token=SLACK_APP_VALUE,
        chat_names=["general"],
        assistant_name="pynchy",
        allow_create=False,
        on_message=on_message or MagicMock(),
        on_chat_metadata=on_chat_metadata or MagicMock(),
    )
    channel.register_allowed_channel("general", "C12345")
    channel.register_allowed_channel("general", "C1")
    return channel
