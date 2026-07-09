"""Connection lifecycle for :class:`DiscordChannel`.

discord.py owns the hard parts — gateway handshake, IDENTIFY/RESUME, heartbeat,
and backoff reconnect — so this collaborator just constructs the client with
the right intents, starts it as a background task, and exposes connect /
disconnect / reconnect / liveness to the host.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

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
    intents.guild_messages = True
    intents.dm_messages = True
    intents.message_content = True  # privileged — must be enabled in the Dev Portal
    intents.guild_reactions = True
    intents.dm_reactions = True
    return intents


class DiscordLifecycle:
    def __init__(self, channel: DiscordChannel) -> None:
        self._channel = channel
        self._task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        ch = self._channel
        ch.shutting_down = False
        client = discord.Client(intents=_intents())
        ch.client = client
        ch.events.register()

        @client.event
        async def on_ready() -> None:  # noqa: RUF029, RUF100 - discord.py event callbacks are async.
            ch.bot_user_id = str(client.user.id) if client.user else ""
            ch.connected = True
            logger.info("Connected to Discord", connection=ch.name, bot_user_id=ch.bot_user_id)

        self._task = asyncio.ensure_future(self._run(client))

    async def _run(self, client: discord.Client) -> None:
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
    def _reconnect_task(self) -> Any:
        return self._task
