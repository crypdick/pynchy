"""Public Slack channel lifecycle behavior at connection boundaries."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.plugins.channels.slack import SlackChannel

_SLACK_BOT_VALUE = "value-a"
_SLACK_APP_VALUE = "value-b"


def _channel() -> SlackChannel:
    return SlackChannel(
        connection_name="test-conn",
        bot_token=_SLACK_BOT_VALUE,
        app_token=_SLACK_APP_VALUE,
        chat_names=[],
        assistant_name="pynchy",
        allow_create=False,
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
    )


def test_is_connected_requires_a_live_socket_task() -> None:
    async def scenario() -> None:
        channel = _channel()
        channel.connected = True
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        channel.handler_task = task

        assert channel.is_connected()

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not channel.is_connected()

    asyncio.run(scenario())


def test_prepare_shutdown_prevents_reconnect_after_socket_exit() -> None:
    async def scenario() -> None:
        channel = _channel()
        channel.connected = True
        channel.prepare_shutdown()

        task = asyncio.create_task(asyncio.sleep(0))
        await task
        channel.handle_socket_mode_exit(task)

        assert channel.reconnect_task is None

    asyncio.run(scenario())


def test_disconnect_suppresses_handler_close_failure_and_cancels_socket_task() -> None:
    async def scenario() -> None:
        channel = _channel()
        channel.connected = True
        handler = MagicMock()
        handler.close_async = AsyncMock(side_effect=OSError("socket already closed"))
        channel.handler = handler
        channel.handler_task = asyncio.create_task(asyncio.sleep(60))

        await channel.disconnect()

        assert not channel.connected
        handler.close_async.assert_awaited_once()
        with contextlib.suppress(asyncio.CancelledError):
            await channel.handler_task
        assert channel.handler_task.cancelled()

    asyncio.run(scenario())


def test_reconnect_suppresses_handler_close_failure_before_connecting() -> None:
    async def scenario() -> None:
        channel = _channel()
        handler = MagicMock()
        handler.close_async = AsyncMock(side_effect=OSError("socket already closed"))
        channel.handler = handler
        channel.handler_task = asyncio.create_task(asyncio.sleep(60))
        channel.connect = AsyncMock()

        await channel.reconnect()

        assert channel.handler is None
        assert channel.handler_task is None
        channel.connect.assert_awaited_once()

    asyncio.run(scenario())


def test_failed_backoff_reconnect_schedules_a_capped_retry() -> None:
    async def scenario() -> None:
        channel = _channel()
        channel.connect = AsyncMock(side_effect=OSError("offline"))
        scheduled = asyncio.create_task(asyncio.sleep(0))

        def schedule_retry(coro, *, name: str) -> asyncio.Task[None]:
            del name
            coro.close()
            return scheduled

        with patch(
            "pynchy.plugins.channels.slack._lifecycle.asyncio.create_task",
            side_effect=schedule_retry,
        ) as create_task:
            await channel.lifecycle.reconnect_with_backoff(0.0)

        assert not channel.connected
        create_task.assert_called_once()
        assert channel.reconnect_task is scheduled
        await scheduled

    asyncio.run(scenario())
