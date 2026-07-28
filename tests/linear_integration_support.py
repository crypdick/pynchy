"""Tests for the built-in Linear MCP integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp.test_utils import TestClient, TestServer

from pynchy.plugins.integrations.linear import build_app

if TYPE_CHECKING:
    from unittest.mock import MagicMock


class FakePostContext:
    def __init__(self, response: MagicMock) -> None:
        self.response = response

    async def __aenter__(self) -> MagicMock:
        return self.response

    async def __aexit__(self, exc_type, exc, _tb) -> None:
        return None


async def start_mcp_client() -> TestClient:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    return client
