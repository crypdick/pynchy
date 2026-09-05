"""Channel plugin activation from flat [connections.<name>] config."""

from __future__ import annotations

import sys
from pathlib import Path  # noqa: TC003 - test context constructs the concrete cache path.
from types import ModuleType
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from pynchy.channels import SlackConnectionSettings, WhatsAppConnectionSettings
from pynchy.config.api import (
    DiscordConnectionConfig,
)
from pynchy.discord import (  # noqa: TC001 - test context exposes the concrete domain value.
    DiscordConnectionSettings,
)
from pynchy.plugins.api import ChannelPluginContext
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin
from pynchy.plugins.channels.slack import SlackChannel, SlackChannelPlugin
from pynchy.plugins.speech.pocket_tts import PocketTtsProvider

SLACK_BOT_ENV = "BOT"
SLACK_APP_ENV = "APP"
DISCORD_BOT_ENV = "DISCORD"
DISCORD_MISSING_ENV = "MISSING_DISCORD"


def _install_module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # noqa: V101  # type: ignore[attr-defined]  # import package marker.
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
neonize_utils.enum = neonize_enum  # noqa: V101


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

from pynchy.plugins.api import Channel  # noqa: E402
from pynchy.plugins.channels.whatsapp import WhatsAppPlugin  # noqa: E402


def _context(
    *,
    speech_synthesizer: Any | None = None,
    discord_connections: dict[str, DiscordConnectionSettings] | None = None,
    discord_audio_cache_dir: Path | None = None,
    slack_connections: dict[str, SlackConnectionSettings] | None = None,
    whatsapp_connections: dict[str, WhatsAppConnectionSettings] | None = None,
) -> ChannelPluginContext:
    return ChannelPluginContext(
        on_message_callback=MagicMock(),
        on_chat_metadata_callback=MagicMock(),
        on_reaction_callback=MagicMock(),
        on_ask_user_answer_callback=MagicMock(),
        on_approval_decision_callback=MagicMock(),
        workspaces=MagicMock(return_value={}),
        send_message=MagicMock(),
        speech_synthesizer=speech_synthesizer,
        discord_connections=discord_connections or {},
        discord_audio_cache_dir=discord_audio_cache_dir,
        slack_connections=slack_connections or {},
        whatsapp_connections=whatsapp_connections or {},
    )


def test_slack_plugin_uses_flat_connection_name_and_type() -> None:
    context = _context(
        slack_connections={
            "synapse": SlackConnectionSettings(
                bot_token_env=SLACK_BOT_ENV,
                app_token_env=SLACK_APP_ENV,
                chat_names=("general",),
                assistant_name="pynchy",
                allow_create=True,
            )
        }
    )

    with patch.dict("os.environ", {"BOT": "xoxb-test", "APP": "xapp-test"}, clear=False):
        channels = SlackChannelPlugin().pynchy_create_channel(context=context)

    assert channels is not None
    assert len(channels) == 1
    assert isinstance(channels[0], SlackChannel)
    assert channels[0].name == "synapse"
    assert channels[0].allow_create is True
    assert channels[0].on_approval_decision is context.on_approval_decision_callback


def test_slack_plugin_returns_none_without_context() -> None:
    assert SlackChannelPlugin().pynchy_create_channel(context=None) is None


def test_slack_plugin_skips_connections_without_required_configuration() -> None:
    context = _context(
        slack_connections={
            "empty-env": SlackConnectionSettings(
                bot_token_env="",
                app_token_env=SLACK_APP_ENV,
                chat_names=("general",),
                assistant_name="pynchy",
                allow_create=False,
            ),
            "empty-chats": SlackConnectionSettings(
                bot_token_env=SLACK_BOT_ENV,
                app_token_env=SLACK_APP_ENV,
                chat_names=(),
                assistant_name="pynchy",
                allow_create=False,
            ),
            "missing-tokens": SlackConnectionSettings(
                bot_token_env=SLACK_BOT_ENV,
                app_token_env=SLACK_APP_ENV,
                chat_names=("general",),
                assistant_name="pynchy",
                allow_create=False,
            ),
        }
    )

    with patch.dict("os.environ", {}, clear=True):
        assert SlackChannelPlugin().pynchy_create_channel(context=context) is None


def test_discord_plugin_uses_flat_connection_name_and_type(tmp_path: Path) -> None:
    config = DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV)
    audio_cache_dir = tmp_path / "discord"

    speech_synthesizer = PocketTtsProvider()
    discord_token = "-".join(("discord", "token"))
    context = _context(
        speech_synthesizer=speech_synthesizer,
        discord_connections={"synapse": config.to_runtime_settings()},
        discord_audio_cache_dir=audio_cache_dir,
    )
    with (
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
        config=config.to_runtime_settings(),
        bot_token=ANY,
        on_message=context.on_message_callback,
        on_chat_metadata=context.on_chat_metadata_callback,
        on_reaction=context.on_reaction_callback,
        on_ask_user_answer=context.on_ask_user_answer_callback,
        on_approval_decision=context.on_approval_decision_callback,
        workspaces=context.workspaces,
        speech_synthesizer=speech_synthesizer,
        transcribe_audio=None,
        process_inbound_audio=None,
        find_chat_jids_by_name=None,
        audio_cache_dir=audio_cache_dir,
    )
    assert channel_class.call_args.kwargs["bot_token"] == discord_token


def test_discord_plugin_skips_empty_or_missing_bot_tokens(tmp_path: Path) -> None:
    empty = _context(
        discord_connections={
            "empty": DiscordConnectionConfig(bot_token_env="").to_runtime_settings()
        },
        discord_audio_cache_dir=tmp_path,
    )
    missing = _context(
        discord_connections={
            "missing": DiscordConnectionConfig(
                bot_token_env=DISCORD_MISSING_ENV
            ).to_runtime_settings()
        },
        discord_audio_cache_dir=tmp_path,
    )

    with patch.dict("os.environ", {}, clear=True):
        assert DiscordChannelPlugin().pynchy_create_channel(context=empty) is None
        assert DiscordChannelPlugin().pynchy_create_channel(context=missing) is None


def test_discord_plugin_requires_an_audio_cache_directory() -> None:
    context = _context(
        discord_connections={
            "synapse": DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV).to_runtime_settings()
        },
    )

    with (
        patch.dict("os.environ", {DISCORD_BOT_ENV: "token"}, clear=False),
        pytest.raises(RuntimeError, match="audio cache directory"),
    ):
        DiscordChannelPlugin().pynchy_create_channel(context=context)


def test_whatsapp_plugin_uses_flat_connection_name_and_type(tmp_path) -> None:
    channel = MagicMock(spec=Channel)
    context = _context(
        whatsapp_connections={
            "phone": WhatsAppConnectionSettings(
                auth_db_path=tmp_path / "phone.db",
                assistant_name="pynchy",
            )
        }
    )

    with (
        patch(
            "pynchy.plugins.channels.whatsapp.WhatsAppChannel",
            return_value=channel,
        ) as channel_cls,
    ):
        channels = WhatsAppPlugin().pynchy_create_channel(context=context)

    assert channels == [channel]
    channel_cls.assert_called_once()
    assert channel_cls.call_args.kwargs["connection_name"] == "phone"


def test_whatsapp_plugin_rejects_duplicate_auth_databases(tmp_path: Path) -> None:
    auth_db_path = tmp_path / "phone.db"
    context = _context(
        whatsapp_connections={
            "phone-one": WhatsAppConnectionSettings(
                auth_db_path=auth_db_path,
                assistant_name="pynchy",
            ),
            "phone-two": WhatsAppConnectionSettings(
                auth_db_path=auth_db_path,
                assistant_name="pynchy",
            ),
        }
    )

    with (
        patch("pynchy.plugins.channels.whatsapp.WhatsAppChannel"),
        pytest.raises(ValueError, match="auth_db_path must be unique"),
    ):
        WhatsAppPlugin().pynchy_create_channel(context=context)
