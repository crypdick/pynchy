"""Public Cop gateway-client response contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.container_manager.security.cop_client import (
    CopGatewayUnavailableError,
    configure_cop_gateway,
    request_inspection,
)


@dataclass(frozen=True)
class _Gateway:
    port: int = 4010
    key: str = "test-key"


def _session_context(*, json_data: object | None = None, text: str = ""):
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = AsyncMock(return_value=json_data)
    response.text = AsyncMock(return_value=text)

    @asynccontextmanager
    async def post(*_args, **_kwargs):
        yield response

    session = AsyncMock()
    session.post = post

    @asynccontextmanager
    async def session_context_manager(*_args, **_kwargs):
        yield session

    return session_context_manager


@pytest.mark.asyncio
async def test_request_inspection_rejects_an_unavailable_gateway() -> None:
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
        pytest.raises(CopGatewayUnavailableError, match="No gateway available"),
    ):
        await request_inspection(system_prompt="Inspect", user_content="content")


@pytest.mark.asyncio
async def test_request_inspection_reads_messages_json_object() -> None:
    configure_cop_gateway(model="cop-test", wire_api="messages")
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(json_data={"content": [{"text": '{"flagged": false}'}]}),
        ),
    ):
        result = await request_inspection(system_prompt="Inspect", user_content="content")

    assert result == {"flagged": False}


@pytest.mark.asyncio
async def test_request_inspection_reads_responses_json_content_parts() -> None:
    configure_cop_gateway(model="cop-test", wire_api="responses")
    payload = (
        '{"output": [{"content": [{"type": "output_text", "text": "{\\"flagged\\": true}"}]}]}'
    )
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(text=payload),
        ),
    ):
        result = await request_inspection(system_prompt="Inspect", user_content="content")

    assert result == {"flagged": True}


@pytest.mark.asyncio
async def test_request_inspection_rejects_non_object_messages_json() -> None:
    configure_cop_gateway(model="cop-test", wire_api="messages")
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(json_data={"content": [{"text": "[]"}]}),
        ),
        pytest.raises(ValueError, match="must be a JSON object"),
    ):
        await request_inspection(system_prompt="Inspect", user_content="content")


@pytest.mark.asyncio
async def test_request_inspection_reads_responses_sse_completion_text() -> None:
    configure_cop_gateway(model="cop-test", wire_api="responses")
    payload = "\n".join(
        [
            "data: 1",
            'data: {"type":"response.output_text.done","text":"{\\"flagged\\":false}"}',
            "data: [DONE]",
        ]
    )
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(text=payload),
        ),
    ):
        result = await request_inspection(system_prompt="Inspect", user_content="content")

    assert result == {"flagged": False}


@pytest.mark.asyncio
async def test_request_inspection_rejects_responses_without_text() -> None:
    configure_cop_gateway(model="cop-test", wire_api="responses")
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(text='data: {"type":"other"}\n\ndata: [DONE]'),
        ),
        pytest.raises(TypeError, match="omitted text content"),
    ):
        await request_inspection(system_prompt="Inspect", user_content="content")


@pytest.mark.asyncio
async def test_request_inspection_rejects_malformed_responses_json_shapes() -> None:
    configure_cop_gateway(model="cop-test", wire_api="responses")
    payload = '{"output": [1, {"content": "wrong"}, {"content": [1, {"type": "other"}]}]}'
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(text=payload),
        ),
        pytest.raises(TypeError, match="omitted text content"),
    ):
        await request_inspection(system_prompt="Inspect", user_content="content")


@pytest.mark.asyncio
async def test_request_inspection_rejects_missing_messages_text() -> None:
    configure_cop_gateway(model="cop-test", wire_api="messages")
    with (
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=_Gateway()),
        patch(
            "pynchy.host.container_manager.security.cop_client.aiohttp.ClientSession",
            _session_context(json_data={"content": [{"type": "text"}]}),
        ),
        pytest.raises(TypeError, match="Messages response omitted text"),
    ):
        await request_inspection(system_prompt="Inspect", user_content="content")
