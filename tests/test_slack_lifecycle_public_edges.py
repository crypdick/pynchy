"""Public Slack channel lifecycle behavior at connection boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import ModuleType
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


def test_disconnect_cancels_a_pending_reconnect_task() -> None:
    async def scenario() -> None:
        channel = _channel()
        reconnect_task = asyncio.create_task(asyncio.sleep(60))
        channel.reconnect_task = reconnect_task

        await channel.disconnect()

        assert channel.reconnect_task is None
        with contextlib.suppress(asyncio.CancelledError):
            await reconnect_task
        assert reconnect_task.cancelled()

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


def test_reconnect_closes_a_handler_without_a_live_socket_task() -> None:
    async def scenario() -> None:
        channel = _channel()
        handler = MagicMock()
        handler.close_async = AsyncMock()
        channel.handler = handler
        channel.connect = AsyncMock()

        await channel.reconnect()

        handler.close_async.assert_awaited_once()
        channel.connect.assert_awaited_once()

    asyncio.run(scenario())


def test_reconnect_connects_when_no_handler_exists() -> None:
    async def scenario() -> None:
        channel = _channel()
        channel.connect = AsyncMock()

        await channel.reconnect()

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


def test_backoff_reconnect_stops_after_connection_or_shutdown() -> None:
    async def scenario() -> None:
        for connected, shutting_down in ((True, False), (False, True)):
            channel = _channel()
            channel.connected = connected
            channel.shutting_down = shutting_down
            channel.connect = AsyncMock()

            await channel.lifecycle.reconnect_with_backoff(0.0)

            channel.connect.assert_not_awaited()

    asyncio.run(scenario())


def test_backoff_retry_handles_a_closing_event_loop() -> None:
    async def scenario() -> None:
        channel = _channel()
        channel.connect = AsyncMock(side_effect=OSError("offline"))

        with patch(
            "pynchy.plugins.channels.slack._lifecycle.asyncio.create_task",
            side_effect=RuntimeError("loop closed"),
        ):
            await channel.lifecycle.reconnect_with_backoff(0.0)

        assert not channel.connected

    asyncio.run(scenario())


def test_connect_continues_when_bot_user_lookup_fails(monkeypatch) -> None:
    class FakeClient:
        async def auth_test(self) -> dict[str, str]:
            raise OSError("auth unavailable")

    class FakeApp:
        def __init__(self, *, token: str) -> None:
            self.token = token
            self.client = FakeClient()

    class FakeHandler:
        def __init__(self, app: FakeApp, app_token: str) -> None:
            self.app = app
            self.app_token = app_token
            self.close_async = AsyncMock()

        async def start_async(self) -> None:
            await asyncio.Event().wait()

    bolt = ModuleType("slack_bolt")
    adapter = ModuleType("slack_bolt.adapter")
    socket_mode = ModuleType("slack_bolt.adapter.socket_mode")
    handler_module = ModuleType("slack_bolt.adapter.socket_mode.async_handler")
    app_module = ModuleType("slack_bolt.async_app")
    handler_module.AsyncSocketModeHandler = FakeHandler
    app_module.AsyncApp = FakeApp
    bolt.adapter = adapter
    adapter.socket_mode = socket_mode
    socket_mode.async_handler = handler_module  # noqa: V101
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", adapter)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", socket_mode)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode.async_handler", handler_module)
    monkeypatch.setitem(sys.modules, "slack_bolt.async_app", app_module)

    async def scenario() -> None:
        channel = _channel()
        channel.sync_allowed_channels = AsyncMock()
        channel.register_inbound_handlers = MagicMock()

        await channel.connect()

        assert channel.connected
        assert not channel.bot_user_id
        await channel.disconnect()

    asyncio.run(scenario())
