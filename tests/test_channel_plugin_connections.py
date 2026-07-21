"""Channel plugin activation from flat [connections.<name>] config."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import ANY, MagicMock, patch

from conftest import make_settings

from pynchy.config.models import (
    CommandCenterConfig,
    DiscordConnectionConfig,
    SlackConnectionConfig,
    WhatsAppConnectionConfig,
)
from pynchy.plugins.channel_runtime import ChannelPluginContext
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin
from pynchy.plugins.channels.slack import SlackChannel, SlackChannelPlugin
from pynchy.plugins.speech.pocket_tts import PocketTtsProvider

SLACK_BOT_ENV = "BOT"
SLACK_APP_ENV = "APP"
DISCORD_BOT_ENV = "DISCORD"


def _install_module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]  # noqa: RUF100 - import package marker.
    sys.modules[name] = module
    return module


neonize = _install_module("neonize", package=True)
aioze = _install_module("neonize.aioze", package=True)
aioze_client = _install_module("neonize.aioze.client")
aioze_events = _install_module("neonize.aioze.events")
neonize_events = _install_module("neonize.events")
neonize_utils = _install_module("neonize.utils", package=True)
neonize_jid = _install_module("neonize.utils.jid")
neonize_enum = _install_module("neonize.utils.enum")

neonize.aioze = aioze
aioze.client = aioze_client
aioze.events = aioze_events
neonize.utils = neonize_utils
neonize_utils.jid = neonize_jid
neonize_utils.enum = neonize_enum


class _NeonizeClient:
    pass


class _ChatPresence:
    CHAT_PRESENCE_COMPOSING = "composing"
    CHAT_PRESENCE_PAUSED = "paused"


class _ChatPresenceMedia:
    CHAT_PRESENCE_MEDIA_TEXT = "text"


aioze_client.NewAClient = _NeonizeClient
neonize_events.ConnectedEv = type("ConnectedEv", (), {})
neonize_events.ConnectFailureEv = type("ConnectFailureEv", (), {})
neonize_events.DisconnectedEv = type("DisconnectedEv", (), {})
neonize_events.LoggedOutEv = type("LoggedOutEv", (), {})
neonize_events.MessageEv = type("MessageEv", (), {})
neonize_events.PairStatusEv = type("PairStatusEv", (), {})
neonize_enum.ChatPresence = _ChatPresence
neonize_enum.ChatPresenceMedia = _ChatPresenceMedia
neonize_jid.Jid2String = lambda jid: getattr(jid, "value", "")
neonize_jid.build_jid = lambda *parts: parts

from pynchy.plugins.channels.whatsapp import WhatsAppPlugin  # noqa: E402
from pynchy.types import Channel  # noqa: E402


def _context(*, speech_synthesizer: Any | None = None) -> ChannelPluginContext:
    return ChannelPluginContext(
        on_message_callback=MagicMock(),
        on_chat_metadata_callback=MagicMock(),
        on_reaction_callback=MagicMock(),
        on_ask_user_answer_callback=MagicMock(),
        on_approval_decision_callback=MagicMock(),
        workspaces=MagicMock(return_value={}),
        send_message=MagicMock(),
        speech_synthesizer=speech_synthesizer,
    )


def test_slack_plugin_uses_flat_connection_name_and_type() -> None:
    settings = make_settings(
        command_center=CommandCenterConfig(connection="synapse"),
        connections={
            "synapse": SlackConnectionConfig(
                bot_token_env=SLACK_BOT_ENV,
                app_token_env=SLACK_APP_ENV,
                chat={"general": {}},
            ),
            "discord-main": DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        },
    )

    context = _context()
    with (
        patch("pynchy.plugins.channels.slack.get_settings", return_value=settings),
        patch.dict("os.environ", {"BOT": "xoxb-test", "APP": "xapp-test"}, clear=False),
    ):
        channels = SlackChannelPlugin().pynchy_create_channel(context=context)

    assert channels is not None
    assert len(channels) == 1
    assert isinstance(channels[0], SlackChannel)
    assert channels[0].name == "synapse"
    assert channels[0].allow_create is True
    assert channels[0].on_approval_decision is context.on_approval_decision_callback


def test_discord_plugin_uses_flat_connection_name_and_type() -> None:
    settings = make_settings(
        connections={
            "synapse": DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
            "slack-main": SlackConnectionConfig(
                bot_token_env=SLACK_BOT_ENV,
                app_token_env=SLACK_APP_ENV,
                chat={"general": {}},
            ),
        }
    )

    speech_synthesizer = PocketTtsProvider()
    discord_token = "-".join(("discord", "token"))
    context = _context(speech_synthesizer=speech_synthesizer)
    with (
        patch("pynchy.plugins.channels.discord.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.channels.discord._plugin.DiscordChannel",
            wraps=DiscordChannel,
        ) as channel_class,
        patch.dict("os.environ", {"DISCORD": discord_token}, clear=False),
    ):
        channels = DiscordChannelPlugin().pynchy_create_channel(context=context)

    assert channels is not None
    assert len(channels) == 1
    assert isinstance(channels[0], DiscordChannel)
    channel_class.assert_called_once_with(
        connection_name="synapse",
        config=settings.connections["synapse"],
        bot_token=ANY,
        on_message=context.on_message_callback,
        on_chat_metadata=context.on_chat_metadata_callback,
        on_reaction=context.on_reaction_callback,
        on_ask_user_answer=context.on_ask_user_answer_callback,
        on_approval_decision=context.on_approval_decision_callback,
        workspaces=context.workspaces,
        speech_synthesizer=speech_synthesizer,
    )
    assert channel_class.call_args.kwargs["bot_token"] == discord_token


def test_whatsapp_plugin_uses_flat_connection_name_and_type(tmp_path) -> None:
    channel = MagicMock(spec=Channel)
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        connections={
            "phone": WhatsAppConnectionConfig(auth_db_path="phone.db"),
            "synapse": DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        },
    )

    with (
        patch("pynchy.plugins.channels.whatsapp.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.channels.whatsapp.WhatsAppChannel",
            return_value=channel,
        ) as channel_cls,
    ):
        channels = WhatsAppPlugin().pynchy_create_channel(context=_context())

    assert channels == [channel]
    channel_cls.assert_called_once()
    assert channel_cls.call_args.kwargs["connection_name"] == "phone"
