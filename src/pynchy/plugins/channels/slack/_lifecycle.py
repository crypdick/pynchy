"""Slack Socket Mode connection lifecycle: connect, disconnect, reconnect.

Split from ``_channel.py`` as a mixin so the channel module stays focused on
transport and message handling.  :class:`SlackChannel` mixes this in; every
method uses the channel's own state (``self._app``, ``self._handler``, the
``self._sync_allowed_channels``/``self._register_handlers`` hooks from
sibling mixins), so the split is behavior-preserving.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from pynchy.logger import logger

if TYPE_CHECKING:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler


class SlackLifecycleMixin:
    """Connection lifecycle for :class:`SlackChannel`."""

    # Declared here so mypy knows the types when analysing this mixin in
    # isolation; the concrete instances are created in ``SlackChannel.__init__``.
    _handler: AsyncSocketModeHandler | None
    _handler_task: asyncio.Task[None] | None
    _reconnect_task: asyncio.Task[None] | None

    async def connect(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self._app = AsyncApp(token=self._bot_token)

        # Cache bot user ID so we can strip self-mentions from inbound text
        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id", "")
        except Exception:
            logger.warning("Failed to resolve bot user ID (mention stripping disabled)")

        await self._sync_allowed_channels()
        self._register_handlers()

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._handler_task = asyncio.create_task(
            self._handler.start_async(), name="slack-socket-mode"
        )
        self._handler_task.add_done_callback(self._on_handler_done)
        self._connected = True
        logger.info(
            "Slack channel connected (Socket Mode)",
            connection=self._connection_name,
            bot_user_id=self._bot_user_id,
        )

    def is_connected(self) -> bool:
        return self._connected and self._handler_task is not None and not self._handler_task.done()

    async def disconnect(self) -> None:
        self._connected = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._handler:
            with contextlib.suppress(Exception):
                await self._handler.close_async()
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
        logger.info("Slack channel disconnected", connection=self._connection_name)

    async def reconnect(self) -> None:
        """Force an immediate reconnect regardless of current state."""
        logger.info("Slack reconnecting (forced)", connection=self._connection_name)
        self._connected = False
        if self._handler:
            with contextlib.suppress(Exception):
                await self._handler.close_async()
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
        self._handler = None
        self._handler_task = None
        await self.connect()

    # ------------------------------------------------------------------
    # Shutdown coordination
    # ------------------------------------------------------------------

    def prepare_shutdown(self) -> None:
        """Signal imminent shutdown — suppress reconnect attempts."""
        self._shutting_down = True

    # ------------------------------------------------------------------
    # Internal: reconnect on unexpected task exit
    # ------------------------------------------------------------------

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        """Called when the Socket Mode handler task exits for any reason."""
        if not self._connected or self._shutting_down:
            return  # clean shutdown or imminent shutdown — don't reconnect
        exc = task.exception() if not task.cancelled() else None
        logger.warning(
            "Slack Socket Mode task exited unexpectedly — scheduling reconnect",
            connection=self._connection_name,
            exc=str(exc) if exc else "cancelled",
        )
        self._connected = False
        coro = self._reconnect_with_backoff()
        try:
            self._reconnect_task = task.get_loop().create_task(coro, name="slack-reconnect")
        except RuntimeError:
            # Event loop is shutting down — can't schedule reconnect.
            coro.close()
            logger.debug("Cannot schedule Slack reconnect — event loop closing")

    async def _reconnect_with_backoff(self, delay: float = 5.0) -> None:
        """Reconnect with exponential backoff, capped at 5 minutes."""
        await asyncio.sleep(delay)
        # Guard: if disconnect() was called while we slept, or another path
        # already reconnected, bail out — otherwise connect() will spawn
        # aiohttp tasks that disconnect() can't cancel (shutdown race).
        if self._connected or self._shutting_down:
            return
        logger.info("Slack attempting reconnect", connection=self._connection_name, delay=delay)
        try:
            self._handler = None
            self._handler_task = None
            await self.connect()
            self._reconnect_task = None
        except Exception as exc:
            logger.warning("Slack reconnect failed, will retry", delay=delay, exc=str(exc))
            self._connected = False
            next_delay = min(delay * 2, 300)
            coro = self._reconnect_with_backoff(next_delay)
            try:
                self._reconnect_task = asyncio.create_task(coro, name="slack-reconnect")
            except RuntimeError:
                coro.close()
                logger.debug("Cannot schedule Slack reconnect retry — event loop closing")
