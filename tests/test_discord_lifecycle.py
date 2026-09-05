"""Tests for Discord lifecycle state coordination."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"


def _channel(*, application_id: str | None = None) -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            application_id=application_id,
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
        audio_cache_dir=Path("data/media/discord"),
    )


class _FakeClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeGatewayClient:
    """discord.py-shaped gateway client controlled by public channel methods."""

    clients: list[_FakeGatewayClient] = []
    start_error: discord.DiscordException | None = None

    def __init__(self, *, intents: object, application_id: int | None = None) -> None:
        self.intents = intents
        self.application_id = application_id
        self.http = object()  # noqa: V101
        self._connection = type("ConnectionState", (), {"_command_tree": None})()
        self.user = type("User", (), {"id": "999"})()
        self.handlers: dict[str, Any] = {}
        self.closed = False
        self._stopped = asyncio.Event()
        self.clients.append(self)

    def event(self, handler: Any) -> Any:
        self.handlers[handler.__name__] = handler
        return handler

    async def start(self, _token: str) -> None:
        if self.start_error is not None:
            raise self.start_error
        await self._stopped.wait()

    async def close(self) -> None:
        self.closed = True
        self._stopped.set()


class _CancellationGatewayClient(_FakeGatewayClient):
    async def close(self) -> None:
        self.closed = True


def test_discord_channel_exposes_lifecycle_state_properties() -> None:
    ch = _channel()

    assert ch.bot_token == DISCORD_BOT_VALUE
    assert ch.connected is False
    assert ch.shutting_down is False

    ch.connected = True
    ch.shutting_down = True

    assert ch.connected is True
    assert ch.shutting_down is True


def test_discord_lifecycle_prepare_shutdown_sets_public_state() -> None:
    ch = _channel()

    ch.prepare_shutdown()

    assert ch.shutting_down is True


@pytest.mark.asyncio
async def test_discord_lifecycle_disconnect_updates_public_state() -> None:
    ch = _channel()
    client = _FakeClosableClient()
    ch.client = client
    ch.connected = True

    await ch.disconnect()

    assert client.closed is True
    assert ch.connected is False
    assert ch.shutting_down is True


@pytest.mark.asyncio
async def test_discord_channel_connects_on_ready_and_stops_through_its_public_api() -> None:
    _FakeGatewayClient.clients = []
    _FakeGatewayClient.start_error = None
    ch = _channel()

    with patch(
        "pynchy.plugins.channels.discord._lifecycle.discord.Client",
        _FakeGatewayClient,
    ):
        await ch.connect()
        client = _FakeGatewayClient.clients[-1]
        await client.handlers["on_ready"]()

        assert ch.bot_user_id == "999"
        assert ch.is_connected() is True

        await ch.disconnect()

    assert client.closed is True
    assert ch.client is None
    assert ch.is_connected() is False


@pytest.mark.asyncio
async def test_discord_channel_connects_with_configured_application_id() -> None:
    _FakeGatewayClient.clients = []
    _FakeGatewayClient.start_error = None
    ch = _channel(application_id="123")

    with patch(
        "pynchy.plugins.channels.discord._lifecycle.discord.Client",
        _FakeGatewayClient,
    ):
        await ch.connect()
        client = _FakeGatewayClient.clients[-1]
        assert client.application_id == 123
        await ch.disconnect()


@pytest.mark.asyncio
async def test_discord_channel_reconnects_through_its_public_api() -> None:
    _FakeGatewayClient.clients = []
    _FakeGatewayClient.start_error = None
    ch = _channel()

    with patch(
        "pynchy.plugins.channels.discord._lifecycle.discord.Client",
        _FakeGatewayClient,
    ):
        await ch.reconnect()
        assert len(_FakeGatewayClient.clients) == 1
        assert ch.client is _FakeGatewayClient.clients[0]
        await ch.disconnect()


@pytest.mark.asyncio
async def test_discord_channel_disconnect_cancels_a_running_gateway_task() -> None:
    _CancellationGatewayClient.clients = []
    _CancellationGatewayClient.start_error = None
    ch = _channel()

    with patch(
        "pynchy.plugins.channels.discord._lifecycle.discord.Client",
        _CancellationGatewayClient,
    ):
        await ch.connect()
        await asyncio.sleep(0)
        await ch.disconnect()

    assert ch.client is None


@pytest.mark.asyncio
async def test_discord_channel_keeps_a_gateway_failure_from_crashing_the_host() -> None:
    _FakeGatewayClient.clients = []
    _FakeGatewayClient.start_error = discord.DiscordException("invalid token")
    ch = _channel()

    with patch(
        "pynchy.plugins.channels.discord._lifecycle.discord.Client",
        _FakeGatewayClient,
    ):
        await ch.connect()
        await asyncio.sleep(0)

        assert ch.is_connected() is False
        await ch.disconnect()


@pytest.mark.asyncio
async def test_discord_gateway_voice_events_reach_the_public_channel_handler() -> None:
    _FakeGatewayClient.clients = []
    _FakeGatewayClient.start_error = None
    ch = _channel()

    with patch(
        "pynchy.plugins.channels.discord._lifecycle.discord.Client",
        _FakeGatewayClient,
    ):
        await ch.connect()
        client = _FakeGatewayClient.clients[-1]
        handler = AsyncMock()
        with patch.object(ch, "handle_voice_state_update", handler):
            await client.handlers["on_voice_state_update"]("member", "before", "after")

        handler.assert_awaited_once_with("member", "before", "after")
        await ch.disconnect()
