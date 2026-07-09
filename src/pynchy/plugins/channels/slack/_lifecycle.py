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
from typing import TYPE_CHECKING

from pynchy.logger import logger

if TYPE_CHECKING:
    from ._channel import SlackChannel
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
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        ch = self._channel
        ch._app = AsyncApp(token=ch._bot_token)

        # Cache bot user ID so we can strip self-mentions from inbound text
        try:
            auth = await ch._app.client.auth_test()
            ch._bot_user_id = auth.get("user_id", "")
        except Exception:  # noqa: BLE001, RUF100 - bot user lookup is best-effort and can be skipped.
            logger.warning("Failed to resolve bot user ID (mention stripping disabled)")

        await ch.allowlist._sync_allowed_channels()
        ch.events._register_handlers()

        ch._handler = AsyncSocketModeHandler(ch._app, ch._app_token)
        ch._handler_task = asyncio.create_task(ch._handler.start_async(), name="slack-socket-mode")
        ch._handler_task.add_done_callback(self._on_handler_done)
        ch._connected = True
        logger.info(
            "Slack channel connected (Socket Mode)",
            connection=ch._connection_name,
            bot_user_id=ch._bot_user_id,
        )

    def is_connected(self) -> bool:
        ch = self._channel
        return ch._connected and ch._handler_task is not None and not ch._handler_task.done()

    async def disconnect(self) -> None:
        ch = self._channel
        ch._connected = False
        if ch._reconnect_task and not ch._reconnect_task.done():
            ch._reconnect_task.cancel()
            ch._reconnect_task = None
        if ch._handler:
            with contextlib.suppress(Exception):
                await ch._handler.close_async()
        if ch._handler_task and not ch._handler_task.done():
            ch._handler_task.cancel()
        logger.info("Slack channel disconnected", connection=ch._connection_name)

    async def reconnect(self) -> None:
        """Force an immediate reconnect regardless of current state."""
        ch = self._channel
        logger.info("Slack reconnecting (forced)", connection=ch._connection_name)
        ch._connected = False
        if ch._handler:
            with contextlib.suppress(Exception):
                await ch._handler.close_async()
        if ch._handler_task and not ch._handler_task.done():
            ch._handler_task.cancel()
        ch._handler = None
        ch._handler_task = None
        await ch.connect()

    # ------------------------------------------------------------------
    # Shutdown coordination
    # ------------------------------------------------------------------

    def prepare_shutdown(self) -> None:
        """Signal imminent shutdown — suppress reconnect attempts."""
        self._channel._shutting_down = True

    # ------------------------------------------------------------------
    # Internal: reconnect on unexpected task exit
    # ------------------------------------------------------------------

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        """Called when the Socket Mode handler task exits for any reason."""
        ch = self._channel
        if not ch._connected or ch._shutting_down:
            return  # clean shutdown or imminent shutdown — don't reconnect
        exc = task.exception() if not task.cancelled() else None
        logger.warning(
            "Slack Socket Mode task exited unexpectedly — scheduling reconnect",
            connection=ch._connection_name,
            exc=str(exc) if exc else "cancelled",
        )
        ch._connected = False
        coro = self._reconnect_with_backoff()
        try:
            ch._reconnect_task = task.get_loop().create_task(coro, name="slack-reconnect")
        except RuntimeError:
            # Event loop is shutting down — can't schedule reconnect.
            coro.close()
            logger.debug("Cannot schedule Slack reconnect — event loop closing")

    async def _reconnect_with_backoff(self, delay: float = 5.0) -> None:
        """Reconnect with exponential backoff, capped at 5 minutes."""
        ch = self._channel
        await asyncio.sleep(delay)
        # Guard: if disconnect() was called while we slept, or another path
        # already reconnected, bail out — otherwise connect() will spawn
        # aiohttp tasks that disconnect() can't cancel (shutdown race).
        if ch._connected or ch._shutting_down:
            return
        logger.info("Slack attempting reconnect", connection=ch._connection_name, delay=delay)
        try:
            ch._handler = None
            ch._handler_task = None
            await ch.connect()
            ch._reconnect_task = None
        except Exception as exc:  # noqa: BLE001, RUF100 - reconnect failures are expected retryable Slack lifecycle errors.
            logger.warning("Slack reconnect failed, will retry", delay=delay, exc=str(exc))
            ch._connected = False
            next_delay = min(delay * 2, 300)
            coro = self._reconnect_with_backoff(next_delay)
            try:
                ch._reconnect_task = asyncio.create_task(coro, name="slack-reconnect")
            except RuntimeError:
                coro.close()
                logger.debug("Cannot schedule Slack reconnect retry — event loop closing")
