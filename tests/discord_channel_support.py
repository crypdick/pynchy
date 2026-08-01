"""Tests for DiscordChannel outbound behavior and protocol conformance.

The discord.py client is faked; these cover the channel's own logic (jid
ownership, chunked sending with safe mention defaults, reaction id handling,
history catch-up filtering) rather than the gateway glue in _lifecycle.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel, PynchyVoiceClient
from pynchy.state.api import get_chat_jids_by_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    import discord
    import pytest

    from pynchy.workspace.api import WorkspaceProfile

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"


def _channel(
    speech_synthesizer: object | None = None,
    config: DiscordConnectionConfig | None = None,
    find_chat_jids_by_name: Callable[[str], Awaitable[list[str]]] | None = get_chat_jids_by_name,
) -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=config or DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
        audio_cache_dir=Path("data/media/discord"),
        find_chat_jids_by_name=find_chat_jids_by_name,
        speech_synthesizer=speech_synthesizer,
    )


def _configured_voice_channel(speech_synthesizer: object | None = None) -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            chat={
                "1": {
                    "name": "Pynchy",
                    "channels": {"2": {"name": "General", "kind": "voice"}},
                }
            },
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        workspaces=lambda: {"discord:voice:2": cast("WorkspaceProfile", object())},
        speech_synthesizer=speech_synthesizer,
    )


class _FakeSendChannel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sends.append((content, kwargs))


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[tuple[str, dict]] = []

    async def edit(self, *, content: str, **kwargs) -> None:
        self.edits.append((content, kwargs))


class _FakeStreamChannel:
    """A channel whose ``send`` returns a message and that can fetch it back."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = 100

    async def send(self, content: str, **kwargs) -> _FakeMessage:
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.messages[msg.id] = msg
        self.sends.append((content, kwargs))
        return msg

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        return self.messages[message_id]


class _FakeTypingChannel:
    def __init__(self) -> None:
        self.typing_calls = 0

    async def typing(self) -> None:
        self.typing_calls += 1


@dataclass(slots=True)
class _FakeThread:
    id: int
    name: str = ""
    parent_id: int | None = None
    archived: bool = False
    parent: object | None = None
    applied_tags: list[object] = field(default_factory=list)
    added_user_ids: list[int] = field(default_factory=list)
    archive_edits: list[bool] = field(default_factory=list)
    tag_edits: list[list[object]] = field(default_factory=list)
    name_edits: list[str] = field(default_factory=list)

    async def add_user(self, user: discord.Object) -> None:
        self.added_user_ids.append(int(user.id))

    async def edit(
        self,
        *,
        archived: bool | None = None,
        applied_tags: list[object] | None = None,
        name: str | None = None,
    ) -> None:
        if archived is not None:
            self.archived = archived
            self.archive_edits.append(archived)
        if applied_tags is not None:
            self.applied_tags = list(applied_tags)
            self.tag_edits.append(list(applied_tags))
        if name is not None:
            self.name = name
            self.name_edits.append(name)


class _FakeThreadParent:
    def __init__(self) -> None:
        self.id = 123
        self.guild = _FakeThreadGuild()
        self.thread_requests: list[tuple[str, object]] = []
        self.created_threads: list[_FakeThread] = []
        self.sent_messages: list[str] = []
        self.archived: list[_FakeThread] = []

    async def create_thread(self, *, name: str, **kwargs: object) -> _FakeThread:
        self.thread_requests.append((name, kwargs["type"]))
        thread = _FakeThread(id=456)
        self.created_threads.append(thread)
        return thread

    async def send(self, content: str) -> None:
        self.sent_messages.append(content)

    async def archived_threads(self, **_kwargs: object):
        for thread in self.archived:
            yield thread


class _FakeThreadGuild:
    def __init__(self) -> None:
        self.threads: list[_FakeThread] = []

    async def active_threads(self) -> list[_FakeThread]:
        return self.threads


class _FakePynchyVoiceClient(PynchyVoiceClient):
    def __init__(self) -> None:
        self.received_listener: object | None = None
        self.played_audio: list[object] = []

    def is_connected(self) -> bool:
        return True

    def start_receiving(self, listener: object) -> None:
        self.received_listener = listener

    def stop_receiving(self) -> None:
        self.received_listener = None

    def play(self, audio: object, *, after) -> None:
        self.played_audio.append(audio)
        after(None)

    def stop(self) -> None:
        pass


class _FakeVoiceChannel:
    id = 2

    def __init__(self, connected: asyncio.Event, release: asyncio.Event) -> None:
        self.connected = connected
        self.release = release
        self.connect_calls = 0
        self.voice_client = _FakePynchyVoiceClient()
        self.guild = _FakeDiscordGuild(1, "Pynchy", [])
        self.name = "General"

    async def connect(self, **_kwargs: object) -> _FakePynchyVoiceClient:
        self.connect_calls += 1
        self.connected.set()
        await self.release.wait()
        return self.voice_client


@dataclass(slots=True)
class _VoiceState:
    channel: object | None


@dataclass
class _VoiceConnectionDecryptHarness:
    dave_session: object
    can_encrypt: bool
    listeners: list[object] = field(default_factory=list)

    def add_socket_listener(self, listener: object) -> None:
        self.listeners.append(listener)

    def remove_socket_listener(self, listener: object) -> None:
        self.listeners.remove(listener)


@dataclass(kw_only=True)
class _VoiceClientDecryptHarness(PynchyVoiceClient):
    _mode: str
    _secret_key: bytes
    _connection: _VoiceConnectionDecryptHarness
    _packet_listener: object | None = None
    _speaker_ids: dict[int, str] = field(default_factory=dict)
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def secret_key(self) -> bytes:
        return self._secret_key


async def _activate_voice_session(
    channel: DiscordChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> _FakePynchyVoiceClient:
    connected = asyncio.Event()
    release = asyncio.Event()
    release.set()
    voice_channel = _FakeVoiceChannel(connected, release)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _voice_channel: {"42": "Alice"},
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    return voice_channel.voice_client


class _FakeUser:
    def __init__(self, dm_channel: object | None = None) -> None:
        self.dm_channel = dm_channel
        self.create_dm_calls = 0
        self.created_dm = dm_channel or _FakeSendChannel()

    async def create_dm(self) -> object:
        self.create_dm_calls += 1
        self.dm_channel = self.created_dm
        return self.created_dm


@dataclass(slots=True)
class _FakeDiscordTextChannel:
    id: int
    name: str


@dataclass(slots=True)
class _FakeDiscordVoiceChannel:
    id: int
    name: str


@dataclass(slots=True)
class _FakeDiscordCategory:
    id: int
    name: str


@dataclass(slots=True)
class _FakeDiscordForum:
    id: int
    name: str
    guild: object
    available_tags: list[object]
    category_id: int | None = None
    edits: list[dict[str, object]] = field(default_factory=list)

    async def edit(self, **options: object) -> _FakeDiscordForum:
        self.edits.append(options)
        if "available_tags" in options:
            self.available_tags = list(cast("list[object]", options["available_tags"]))
        category = options.get("category")
        if category is not None:
            self.category_id = int(category.id)
        return self


class _FakeDiscordUser:
    def __init__(self, user_id: int, name: str, *, display_name: str | None = None) -> None:
        self.id = user_id
        self.name = name
        self.display_name = display_name or name
        self.global_name = display_name

    def __str__(self) -> str:
        return self.name


class _FakeDiscordGuild:
    def __init__(
        self,
        guild_id: int,
        name: str,
        channels: list[_FakeDiscordTextChannel],
        members: list[_FakeDiscordUser] | None = None,
        voice_channels: list[_FakeDiscordVoiceChannel] | None = None,
        forums: list[_FakeDiscordForum] | None = None,
        categories: list[_FakeDiscordCategory] | None = None,
    ) -> None:
        self.id = guild_id
        self.name = name
        self.text_channels = channels
        self.members = members or []
        self.voice_channels = voice_channels or []
        self.forums = forums or []
        self.categories = categories or []
        self.created: list[str] = []

    async def create_text_channel(self, name: str, **kwargs) -> _FakeDiscordTextChannel:
        self.created.append(name)
        channel = _FakeDiscordTextChannel(789, name)
        self.text_channels.append(channel)
        return channel

    async def create_category(self, name: str, **_kwargs: object) -> _FakeDiscordCategory:
        category = _FakeDiscordCategory(890, name)
        self.categories.append(category)
        return category

    async def create_forum(
        self,
        name: str,
        *,
        category: _FakeDiscordCategory | None,
        available_tags: list[object],
        **_kwargs: object,
    ) -> _FakeDiscordForum:
        forum = _FakeDiscordForum(
            id=891,
            name=name,
            guild=self,
            available_tags=available_tags,
            category_id=category.id if category is not None else None,
        )
        self.forums.append(forum)
        return forum


class _FakeDiscordClient:
    def __init__(
        self, guilds: list[_FakeDiscordGuild], users: list[_FakeDiscordUser] | None = None
    ) -> None:
        self.guilds = guilds
        self.users = users or []

    def get_guild(self, guild_id: int) -> _FakeDiscordGuild | None:
        return next((guild for guild in self.guilds if guild.id == guild_id), None)

    async def fetch_guild(self, guild_id: int) -> _FakeDiscordGuild | None:
        return self.get_guild(guild_id)

    def get_all_members(self):
        for guild in self.guilds:
            yield from guild.members


@dataclass
class _DirectMessageClient:
    """The narrow discord.Client surface used by direct-message resolution."""

    get_user: object
    fetch_user: object


@dataclass
class _HistoryAuthor:
    id: str
    bot: bool
    display_name: str


@dataclass
class _HistoryChannel:
    id: str
    name: str | None = None
    parent: object | None = None
    parent_id: str | None = None


@dataclass
class _HistoryMessage:
    """SDK-shaped input that exercises Discord's parser at the history boundary."""

    id: str
    author: _HistoryAuthor
    channel: _HistoryChannel
    content: str
    created_at: datetime
    guild: object | None = None
    attachments: tuple[object, ...] = ()
    reference: object | None = None
    message_snapshots: tuple[object, ...] = ()
    mentions: tuple[object, ...] = ()
    type: object | None = None
