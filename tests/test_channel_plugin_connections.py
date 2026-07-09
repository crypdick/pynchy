"""Channel plugin activation from flat [connections.<name>] config."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from conftest import make_settings

from pynchy.config.models import (
    CommandCenterConfig,
    DiscordConnectionConfig,
    SlackConnectionConfig,
    WhatsAppConnectionConfig,
)
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin
from pynchy.plugins.channels.slack import SlackChannel, SlackChannelPlugin

_NEONIZE_MODULES = [
    "neonize",
    "neonize.aioze",
    "neonize.aioze.client",
    "neonize.aioze.events",
    "neonize.events",
    "neonize.proto",
    "neonize.proto.Neonize_pb2",
    "neonize.utils",
    "neonize.utils.jid",
    "neonize.utils.enum",
]
_neonize_mocks: dict[str, ModuleType] = {}
for _mod_name in _NEONIZE_MODULES:
    if _mod_name not in sys.modules:
        _neonize_mocks[_mod_name] = MagicMock()
        sys.modules[_mod_name] = _neonize_mocks[_mod_name]

from pynchy.plugins.channels.whatsapp import WhatsAppPlugin  # noqa: E402


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        on_message_callback=MagicMock(),
        on_chat_metadata_callback=MagicMock(),
        on_reaction_callback=MagicMock(),
        on_ask_user_answer_callback=MagicMock(),
        workspaces=MagicMock(return_value={}),
    )


def test_slack_plugin_uses_flat_connection_name_and_type() -> None:
    settings = make_settings(
        command_center=CommandCenterConfig(connection="synapse"),
        connections={
            "synapse": SlackConnectionConfig(
                bot_token_env="BOT",
                app_token_env="APP",
                chat={"general": {}},
            ),
            "discord-main": DiscordConnectionConfig(bot_token_env="DISCORD"),
        },
    )

    with (
        patch("pynchy.plugins.channels.slack.get_settings", return_value=settings),
        patch.dict("os.environ", {"BOT": "xoxb-test", "APP": "xapp-test"}, clear=False),
    ):
        channels = SlackChannelPlugin().pynchy_create_channel(context=_context())

    assert channels is not None
    assert len(channels) == 1
    assert isinstance(channels[0], SlackChannel)
    assert channels[0].name == "synapse"
    assert channels[0]._allow_create is True  # allow: private-test-imports


def test_discord_plugin_uses_flat_connection_name_and_type() -> None:
    settings = make_settings(
        connections={
            "synapse": DiscordConnectionConfig(bot_token_env="DISCORD"),
            "slack-main": SlackConnectionConfig(
                bot_token_env="BOT",
                app_token_env="APP",
                chat={"general": {}},
            ),
        }
    )

    with (
        patch("pynchy.plugins.channels.discord.get_settings", return_value=settings),
        patch.dict("os.environ", {"DISCORD": "discord-token"}, clear=False),
    ):
        channels = DiscordChannelPlugin().pynchy_create_channel(context=_context())

    assert channels is not None
    assert len(channels) == 1
    assert isinstance(channels[0], DiscordChannel)
    assert channels[0].name == "synapse"


def test_whatsapp_plugin_uses_flat_connection_name_and_type(tmp_path) -> None:
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        connections={
            "phone": WhatsAppConnectionConfig(auth_db_path="phone.db"),
            "synapse": DiscordConnectionConfig(bot_token_env="DISCORD"),
        },
    )

    with (
        patch("pynchy.plugins.channels.whatsapp.get_settings", return_value=settings),
        patch("pynchy.plugins.channels.whatsapp.WhatsAppChannel") as channel_cls,
    ):
        channels = WhatsAppPlugin().pynchy_create_channel(context=_context())

    assert channels == [channel_cls.return_value]
    channel_cls.assert_called_once()
    assert channel_cls.call_args.kwargs["connection_name"] == "phone"
