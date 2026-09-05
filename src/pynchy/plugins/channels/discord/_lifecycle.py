"""Connection lifecycle for :class:`DiscordChannel`.

discord.py owns the hard parts — gateway handshake, IDENTIFY/RESUME, heartbeat,
and backoff reconnect — so this collaborator just constructs the client with
the right intents, starts it as a background task, and exposes connect /
disconnect / reconnect / liveness to the host.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import discord

from pynchy.logger import logger

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    # See _events.py: circular runtime import avoided; beartype resolves the
    # forward ref to this permissive substitute at call time.
    DiscordChannel = object


def _intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True  # noqa: V101
    intents.dm_messages = True  # noqa: V101
    intents.message_content = True  # noqa: V101  # privileged — must be enabled in the Dev Portal
    intents.guild_reactions = True  # noqa: V101
    intents.dm_reactions = True  # noqa: V101
    intents.voice_states = True  # noqa: V101
    return intents


@runtime_checkable
class _GatewayClient(Protocol):
    async def start(self, token: str) -> None: ...


class DiscordLifecycle:
    def __init__(self, channel: DiscordChannel) -> None:
        self._channel = channel
        self._task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        ch = self._channel
        ch.shutting_down = False
        if ch.config.application_id is None:
            client = discord.Client(intents=_intents())
        else:
            client = discord.Client(
                intents=_intents(),
                application_id=int(ch.config.application_id),
            )
        ch.client = client
        ch.events.register()

        @client.event
        async def on_ready() -> None:  # discord.py event callbacks are async.
            ch.bot_user_id = str(client.user.id) if client.user else ""
            ch.connected = True
            logger.info("Connected to Discord", connection=ch.name, bot_user_id=ch.bot_user_id)
            await ch.events.sync_application_commands()
            await ch.voice.on_ready()

        @client.event
        async def on_voice_state_update(
            member: object, before: object, after: object
        ) -> None:  # discord.py event callbacks are async.
            await ch.handle_voice_state_update(member, before, after)

        self._task = asyncio.ensure_future(self._run(client))

    async def _run(self, client: _GatewayClient) -> None:
        try:
            await client.start(self._channel.bot_token)
        except asyncio.CancelledError:
            raise
        except discord.DiscordException as exc:
            # Background gateway task: any startup/runtime failure (auth,
            # disallowed intents, connection drop) is logged rather than left
            # to crash an orphaned task; the host watchdog drives reconnect.
            logger.warning("Discord client stopped", connection=self._channel.name, err=str(exc))
        finally:
            self._channel.connected = False

    def is_connected(self) -> bool:
        ch = self._channel
        return ch.connected and self._task is not None and not self._task.done()

    async def disconnect(self) -> None:
        ch = self._channel
        ch.shutting_down = True
        ch.connected = False
        if ch.client is not None:
            await ch.client.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        ch.client = None

    async def reconnect(self) -> None:
        await self.disconnect()
        await self.connect()

    def prepare_shutdown(self) -> None:
        self._channel.shutting_down = True

    # Exposed for symmetry with other channels; discord.py handles retries.
    def _reconnect_task(self) -> asyncio.Task[None] | None:
        return self._task
