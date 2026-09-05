"""Slack Socket Mode connection lifecycle: connect, disconnect, reconnect.

A composed collaborator of :class:`SlackChannel` (not a mixin). The channel
constructs one of these and delegates its lifecycle protocol methods to it.
The collaborator holds a back-reference to the channel because the connection
state it drives (``_app``, ``_handler``, ``_bot_user_id``) is late-bound and
reassigned across connects/reconnects — it must be read and written live on
the channel, not snapshotted at construction.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

from pynchy.logger import logger

RECONNECT_INITIAL_DELAY_SECONDS = 5.0

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from ._channel import SlackChannel, _SlackApp
else:
    # beartype resolves the ``channel: SlackChannel`` forward ref at call time
    # from this module's globals. ``_channel`` imports this module, so a real
    # runtime import would be circular — bind a permissive substitute so the
    # forward ref resolves (mypy uses the real type from the branch above).
    SlackChannel = object


class SlackLifecycle:
    """Connection lifecycle for :class:`SlackChannel`."""

    def __init__(self, channel: SlackChannel) -> None:
        self._channel = channel

    async def connect(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import (  # noqa: PLC0415 - optional Slack SDK loaded only when Slack connects.
            AsyncSocketModeHandler,
        )
        from slack_bolt.async_app import (  # noqa: PLC0415 - optional Slack SDK loaded only when Slack connects.
            AsyncApp,
        )

        ch = self._channel
        app = AsyncApp(token=ch.bot_token)
        # The local protocol deliberately exposes only the Slack surface that
        # collaborators consume; the SDK's wider client signature is not
        # structurally assignable to that narrowed protocol.
        ch.slack_app = cast("_SlackApp", app)

        # Cache bot user ID so we can strip self-mentions from inbound text
        try:
            auth = await ch.slack_app.client.auth_test()
            ch.bot_user_id = auth.get("user_id", "")
        except Exception:  # noqa: BLE001 - bot user lookup is best-effort and can be skipped.
            logger.warning("Failed to resolve bot user ID (mention stripping disabled)")

        await ch.sync_allowed_channels()
        ch.register_inbound_handlers()

        ch.handler = AsyncSocketModeHandler(app, ch.app_token)
        start_socket_mode = cast("Callable[[], Coroutine[Any, Any, None]]", ch.handler.start_async)
        ch.handler_task = asyncio.create_task(start_socket_mode(), name="slack-socket-mode")
        ch.handler_task.add_done_callback(ch.handle_socket_mode_exit)
        ch.connected = True
        logger.info(
            "Slack channel connected (Socket Mode)",
            connection=ch.connection_name,
            bot_user_id=ch.bot_user_id,
        )

    def is_connected(self) -> bool:
        ch = self._channel
        return ch.connected and ch.handler_task is not None and not ch.handler_task.done()

    async def disconnect(self) -> None:
        ch = self._channel
        ch.connected = False
        if ch.reconnect_task and not ch.reconnect_task.done():
            ch.reconnect_task.cancel()
            ch.reconnect_task = None
        if ch.handler:
            with contextlib.suppress(Exception):
                await ch.handler.close_async()
        if ch.handler_task and not ch.handler_task.done():
            ch.handler_task.cancel()
        logger.info("Slack channel disconnected", connection=ch.connection_name)

    async def reconnect(self) -> None:
        """Force an immediate reconnect regardless of current state."""
        ch = self._channel
        logger.info("Slack reconnecting (forced)", connection=ch.connection_name)
        ch.connected = False
        if ch.handler:
            with contextlib.suppress(Exception):
                await ch.handler.close_async()
        if ch.handler_task and not ch.handler_task.done():
            ch.handler_task.cancel()
        ch.handler = None
        ch.handler_task = None
        await ch.connect()

    # ------------------------------------------------------------------
    # Shutdown coordination
    # ------------------------------------------------------------------

    def prepare_shutdown(self) -> None:
        """Signal imminent shutdown — suppress reconnect attempts."""
        self._channel.shutting_down = True

    # ------------------------------------------------------------------
    # Internal: reconnect on unexpected task exit
    # ------------------------------------------------------------------

    def on_handler_done(self, task: asyncio.Task[None]) -> None:
        self._on_handler_done(task)

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        """Called when the Socket Mode handler task exits for any reason."""
        ch = self._channel
        if not ch.connected or ch.shutting_down:
            return  # clean shutdown or imminent shutdown — don't reconnect
        exc = task.exception() if not task.cancelled() else None
        logger.warning(
            "Slack Socket Mode task exited unexpectedly — scheduling reconnect",
            connection=ch.connection_name,
            exc=str(exc) if exc else "cancelled",
        )
        ch.connected = False
        coro = self._reconnect_with_backoff(RECONNECT_INITIAL_DELAY_SECONDS)
        try:
            ch.reconnect_task = task.get_loop().create_task(coro, name="slack-reconnect")
        except RuntimeError:
            # Event loop is shutting down — can't schedule reconnect.
            coro.close()
            logger.debug("Cannot schedule Slack reconnect — event loop closing")

    async def reconnect_with_backoff(self, delay: float = 5.0) -> None:  # noqa: V105
        await self._reconnect_with_backoff(delay)

    async def _reconnect_with_backoff(self, delay: float = 5.0) -> None:
        """Reconnect with exponential backoff, capped at 5 minutes."""
        ch = self._channel
        await asyncio.sleep(delay)
        # Guard: if disconnect() was called while we slept, or another path
        # already reconnected, bail out — otherwise connect() will spawn
        # aiohttp tasks that disconnect() can't cancel (shutdown race).
        if ch.connected or ch.shutting_down:
            return
        logger.info("Slack attempting reconnect", connection=ch.connection_name, delay=delay)
        try:
            ch.handler = None
            ch.handler_task = None
            await ch.connect()
            ch.reconnect_task = None
        except Exception as exc:  # noqa: BLE001 - reconnect failures are expected retryable Slack lifecycle errors.
            logger.warning("Slack reconnect failed, will retry", delay=delay, exc=str(exc))
            ch.connected = False
            next_delay = min(delay * 2, 300)
            coro = self._reconnect_with_backoff(next_delay)
            try:
                ch.reconnect_task = asyncio.create_task(coro, name="slack-reconnect")
            except RuntimeError:
                coro.close()
                logger.debug("Cannot schedule Slack reconnect retry — event loop closing")
