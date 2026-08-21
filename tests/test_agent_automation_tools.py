"""Public MCP contracts for config-backed automation tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.agent_tools import (
    AgentToolRuntime,
    call_tool,
    mcp_server,
    use_agent_tool_runtime,
)


def _runtime(tmp_path: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="test@g.us",
        group_folder="test-group",
        is_admin=True,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
    )


def _request_payload(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return envelope, envelope["payload"]


async def _call_tool_over_mcp(name: str, arguments: dict[str, object]) -> CallToolResult:
    async with create_connected_server_and_client_session(mcp_server) as client:
        return await client.call_tool(name, arguments)


@pytest.fixture(autouse=True)
def agent_tool_runtime(tmp_path: Path):
    with use_agent_tool_runtime(_runtime(tmp_path)):
        yield


@pytest.mark.asyncio
@pytest.mark.action("automation.list")
async def test_list_automations_reads_config_definitions(monkeypatch) -> None:
    request = AsyncMock(
        return_value=[
            TextContent(
                type="text",
                text='{"automations":[{"name":"daily","prompt":"check"}]}',
            )
        ]
    )
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_automations.ipc_service_request",
        request,
    )

    result = await _call_tool_over_mcp("list_automations", {})

    assert result.structuredContent == {"automations": [{"name": "daily", "prompt": "check"}]}
    request.assert_awaited_once_with(
        "automation_status",
        {},
        response_timeout_seconds=10,
        type_override="automation_status",
    )


@pytest.mark.asyncio
@pytest.mark.action("automation.read")
async def test_get_automation_reads_definition(monkeypatch) -> None:
    request = AsyncMock(return_value=[TextContent(type="text", text='{"name":"daily"}')])
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_automations.ipc_service_request",
        request,
    )

    result = await call_tool("get_automation", {"name": "daily"})

    assert result[0].text == '{"name":"daily"}'
    request.assert_awaited_once_with(
        "automation_definition",
        {"name": "daily"},
        response_timeout_seconds=10,
        type_override="automation_definition",
    )


@pytest.mark.asyncio
@pytest.mark.action("automation.create")
async def test_create_automation_writes_config_mutation_request(tmp_path: Path) -> None:
    result = await call_tool(
        "create_automation",
        {
            "name": "daily",
            "workspace": "health",
            "prompt": "check",
            "schedule": "0 9 * * *",
        },
    )

    assert "awaiting approval" in result[0].text.lower()
    envelope, payload = _request_payload(next((tmp_path / "requests").glob("*.json")))
    assert envelope["kind"] == "create_automation"
    assert payload["workspace"] == "health"


@pytest.mark.asyncio
@pytest.mark.action("automation.update")
async def test_update_automation_requires_a_change() -> None:
    result = await call_tool("update_automation", {"name": "daily"})

    assert isinstance(result, CallToolResult)
    assert result.isError is True


@pytest.mark.asyncio
@pytest.mark.action("automation.pause")
async def test_pause_automation_writes_request(tmp_path: Path) -> None:
    result = await call_tool("pause_automation", {"name": "daily"})

    assert "awaiting approval" in result[0].text.lower()
    envelope, payload = _request_payload(next((tmp_path / "requests").glob("*.json")))
    assert envelope["kind"] == "pause_automation"
    assert payload == {"name": "daily"}


@pytest.mark.asyncio
@pytest.mark.action("automation.resume")
async def test_resume_automation_writes_request() -> None:
    result = await call_tool("resume_automation", {"name": "daily"})

    assert "awaiting approval" in result[0].text.lower()


@pytest.mark.asyncio
@pytest.mark.action("automation.delete")
async def test_delete_automation_writes_request() -> None:
    result = await call_tool("delete_automation", {"name": "daily"})

    assert "awaiting approval" in result[0].text.lower()
