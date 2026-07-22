"""Behavioral tests for the messaging source-health agent tool."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from agent_runner.agent_tools import (
    AgentToolRuntime,
    call_tool,
    list_tools,
    use_agent_tool_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

_PERSONAL_SOURCES = ["whatsapp", "signal", "google_messages"]


async def _capture_request(ipc_dir: Path) -> dict[str, object]:
    request_dir = ipc_dir / "requests"
    for _ in range(50):
        request_files = list(request_dir.glob("*.json"))
        if request_files:
            request = json.loads(request_files[0].read_text(encoding="utf-8"))
            response_path = ipc_dir / "responses" / f"{request['request_id']}.json"
            response_path.write_text(json.dumps({"result": {"sources": []}}))
            return request
        await asyncio.sleep(0.02)
    raise AssertionError("messaging source-health tool never wrote an IPC request")


@pytest.fixture
def agent_tool_runtime(tmp_path: Path):
    (tmp_path / "responses").mkdir()
    with use_agent_tool_runtime(
        AgentToolRuntime(
            chat_jid="test",
            group_folder="chat-manager",
            is_admin=False,
            is_scheduled_task=False,
            ipc_dir=tmp_path,
        )
    ):
        yield tmp_path


@pytest.mark.asyncio
async def test_omitted_sources_default_to_personal_only(agent_tool_runtime: Path) -> None:
    responder = asyncio.create_task(_capture_request(agent_tool_runtime))

    await asyncio.wait_for(call_tool("messaging_source_health", {}), timeout=10)
    request = await responder

    assert request["kind"] == "messaging_source_health"
    assert request["payload"] == {"sources": _PERSONAL_SOURCES}


@pytest.mark.asyncio
async def test_explicit_configured_channel_is_preserved(agent_tool_runtime: Path) -> None:
    responder = asyncio.create_task(_capture_request(agent_tool_runtime))

    await asyncio.wait_for(
        call_tool("messaging_source_health", {"sources": ["discord"]}), timeout=10
    )
    request = await responder

    assert request["payload"] == {"sources": ["discord"]}


@pytest.mark.asyncio
async def test_schema_documents_the_personal_default() -> None:
    tools = {tool.name: tool for tool in await list_tools()}
    source_health = tools["messaging_source_health"]

    assert source_health.inputSchema["properties"]["sources"]["default"] == _PERSONAL_SOURCES
    assert "checks only WhatsApp, Signal, and Google Messages" in source_health.description
