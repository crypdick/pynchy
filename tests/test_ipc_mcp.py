"""Tests for src/pynchy/agent/agent_runner/src/agent_runner/agent_tools/.

Tests schedule validation, tool authorization, messaging, and task listing logic.
"""

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
    list_tools,
    mcp_server,
    use_agent_tool_runtime,
)

from pynchy.actions.api import ACTION_SPECS, ActionTransport


def _read_request_file(path: Path) -> tuple[dict, dict]:
    """Read a canonical request envelope and return (envelope, payload)."""
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return envelope, envelope["payload"]


def _runtime(tmp_path: Path, **overrides: object) -> AgentToolRuntime:
    values: dict[str, object] = {
        "chat_jid": "test@g.us",
        "group_folder": "test-group",
        "is_admin": True,
        "is_scheduled_task": False,
        "ipc_dir": tmp_path,
    }
    values.update(overrides)
    return AgentToolRuntime(**values)


async def _call_tool_over_mcp(name: str, arguments: dict) -> CallToolResult:
    """Exercise SDK normalization and output-schema validation over the MCP wire path."""
    async with create_connected_server_and_client_session(mcp_server) as client:
        return await client.call_tool(name, arguments)


@pytest.fixture(autouse=True)
def agent_tool_runtime(tmp_path: Path):
    """Run every tool call with an explicit public agent-tools runtime."""
    with use_agent_tool_runtime(_runtime(tmp_path)):
        yield


# ---------------------------------------------------------------------------
# call_tool: register_group authorization
# ---------------------------------------------------------------------------


class TestRegisterGroupAuth:
    """Test register_group admin-only authorization."""

    @pytest.mark.asyncio
    async def test_non_admin_register_group_rejected(self, tmp_path):
        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False, group_folder="non-admin")):
            result = await call_tool(
                "register_group",
                {
                    "jid": "new@g.us",
                    "name": "New Group",
                    "folder": "new-group",
                    "trigger": "@bot",
                },
            )
        assert hasattr(result, "isError")
        assert result.isError is True
        assert "admin" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_admin_register_group_accepted(self):
        result = await call_tool(
            "register_group",
            {
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@bot",
            },
        )
        assert isinstance(result, list)
        assert "registered" in result[0].text.lower()


# ---------------------------------------------------------------------------
# call_tool: deploy_changes authorization
# ---------------------------------------------------------------------------


class TestDeployAuth:
    """Test deploy_changes admin-only authorization."""

    @pytest.mark.asyncio
    async def test_non_admin_deploy_rejected(self, tmp_path):
        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False)):
            result = await call_tool("deploy_changes", {})
        assert hasattr(result, "isError")
        assert result.isError is True
        assert "admin" in result.content[0].text.lower()


@pytest.mark.action("message.outbound.queue")
class TestSendMessage:
    """Test send_message tool."""

    @pytest.mark.asyncio
    async def test_basic_send(self, tmp_path):

        result = await call_tool("send_message", {"text": "Hello world"})

        assert isinstance(result, list)
        assert "sent" in result[0].text.lower()

        files = list((tmp_path / "messages").glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["text"] == "Hello world"
        assert data["chatJid"] == "test@g.us"
        assert data["type"] == "message"

    @pytest.mark.asyncio
    async def test_send_with_sender(self, tmp_path):

        await call_tool("send_message", {"text": "Update", "sender": "Researcher"})

        files = list((tmp_path / "messages").glob("*.json"))
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["sender"] == "Researcher"


@pytest.mark.action("message.source.health")
class TestMessagingSourceHealth:
    """Test the source-health tool's request contract."""

    @pytest.mark.asyncio
    async def test_uses_personal_sources_by_default(self, monkeypatch):
        request = AsyncMock(return_value=[TextContent(type="text", text='{"sources": []}')])
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_messaging.ipc_service_request", request
        )

        result = await call_tool("messaging_source_health", {})

        assert isinstance(result, list)
        assert result[0].text == '{"sources": []}'
        request.assert_awaited_once_with(
            "messaging_source_health",
            {"sources": ["whatsapp", "signal", "google_messages"]},
            type_override="messaging_source_health",
        )

    @pytest.mark.asyncio
    async def test_passes_explicit_sources_to_host(self, monkeypatch):
        request = AsyncMock(
            return_value=[TextContent(type="text", text='{"sources": ["discord"]}')]
        )
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_messaging.ipc_service_request", request
        )

        await call_tool("messaging_source_health", {"sources": ["discord"]})

        request.assert_awaited_once_with(
            "messaging_source_health",
            {"sources": ["discord"]},
            type_override="messaging_source_health",
        )


@pytest.mark.action("task.list")
class TestListTasks:
    """Test list_tasks tool behavior."""

    @pytest.mark.asyncio
    async def test_host_error_returns_compact_error_without_snapshot(self, monkeypatch):
        request = AsyncMock(
            return_value=[
                TextContent(type="text", text="Error: host status unavailable " + ("x" * 10_000))
            ]
        )
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_tasks.ipc_service_request",
            request,
        )

        result = await _call_tool_over_mcp("list_tasks", {})
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert isinstance(result.content[0], TextContent)
        assert "host status unavailable" in result.content[0].text
        assert "No complete scheduled-work inventory is available" in result.content[0].text
        assert len(result.content[0].text) < 320
        request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_compact_complete_structured_live_status(self, monkeypatch):
        live_status = {
            "tasks": [
                {
                    "id": "t1",
                    "group": "admin",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                    "status": "paused",
                    "next_run": None,
                    "last_result": "Blocked: provider unavailable",
                    "orchestration": {
                        "state": "unavailable",
                        "error": "Temporal unavailable",
                    },
                    "run_health": {
                        "last_status": "error",
                        "consecutive_failures": 2,
                    },
                    "health_reasons": [
                        "paused",
                        "recent_failure",
                        "scheduler_error",
                        "failure_shaped_result",
                    ],
                }
            ],
            "host_jobs": [
                {
                    "id": "h1",
                    "name": "backup",
                    "schedule_type": "cron",
                    "schedule_value": "0 1 * * *",
                    "status": "active",
                    "enabled": True,
                    "next_run": "2026-07-23T08:00:00+00:00",
                    "orchestration": {"state": "scheduled", "error": None},
                    "health_reasons": [],
                }
            ],
        }
        request = AsyncMock(
            return_value=[TextContent(type="text", text=json.dumps(live_status, indent=2))]
        )
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_tasks.ipc_service_request",
            request,
        )

        result = await _call_tool_over_mcp("list_tasks", {})
        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[0], TextContent)
        text = result.content[0].text
        payload = json.loads(text)
        assert payload == result.structuredContent
        assert payload["completeness"]["complete_for_scope"] is True
        assert payload["completeness"]["omitted_populations"] == [
            "static config or plugin host schedules",
            "Temporal schedules without a visible database-backed definition",
        ]
        assert payload["counts"] == {"tasks": 1, "host_jobs": 1}
        task = payload["tasks"][0]
        assert task["id"] == "t1"
        assert task["last_result"] == "Blocked: provider unavailable"
        assert task["consecutive_failures"] == 2
        assert task["orchestration_error"] == "Temporal unavailable"
        assert task["health_reasons"] == [
            "paused",
            "recent_failure",
            "scheduler_error",
            "failure_shaped_result",
        ]
        assert payload["host_jobs"][0]["id"] == "h1"
        assert "\n" not in text
        request.assert_awaited_once_with(
            "list_tasks",
            {},
            response_timeout_seconds=10,
            type_override="task_status",
        )

    @pytest.mark.asyncio
    async def test_live_status_is_parseable_and_preserves_tasks(self, monkeypatch):
        tasks = [
            {
                "id": f"task-{index}",
                "group": "admin",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "next_run": "2026-07-23T16:00:00+00:00",
                "last_result": f"result-{index} " + ("x" * 1000),
                "orchestration": {"state": "scheduled", "error": None},
                "health_reasons": [],
                "run_health": {"last_status": "success", "consecutive_failures": 0},
            }
            for index in range(47)
        ]
        live_status = {"tasks": tasks, "host_jobs": []}
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_tasks.ipc_service_request",
            AsyncMock(
                return_value=[TextContent(type="text", text=json.dumps(live_status, indent=2))]
            ),
        )

        result = await _call_tool_over_mcp("list_tasks", {})

        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[0], TextContent)
        text = result.content[0].text
        payload = json.loads(text)
        assert payload == result.structuredContent
        assert payload["counts"] == {"tasks": 47, "host_jobs": 0}
        assert [task["id"] for task in payload["tasks"]] == [f"task-{index}" for index in range(47)]

    @pytest.mark.asyncio
    async def test_live_status_returns_all_rows_beyond_former_global_bounds(self, monkeypatch):
        tasks = [
            {
                "id": f"task-{index}",
                "group": "admin",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "next_run": "2026-07-23T16:00:00+00:00",
                "last_result": "Completed",
                "orchestration": {"state": "scheduled", "error": None},
                "health_reasons": [],
                "run_health": {"last_status": "success", "consecutive_failures": 0},
            }
            for index in range(65)
        ]
        host_jobs = [
            {
                "id": f"host-{index}",
                "name": f"Host job {index}",
                "schedule_type": "cron",
                "schedule_value": "0 1 * * *",
                "status": "active",
                "enabled": True,
                "next_run": "2026-07-23T08:00:00+00:00",
                "orchestration": {"state": "scheduled", "error": None},
                "health_reasons": [],
            }
            for index in range(33)
        ]
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_tasks.ipc_service_request",
            AsyncMock(
                return_value=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "tasks": tasks,
                                "host_jobs": host_jobs,
                            }
                        ),
                    )
                ]
            ),
        )

        result = await _call_tool_over_mcp("list_tasks", {})

        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        payload = json.loads(result.content[0].text)
        assert payload == result.structuredContent
        assert payload["counts"] == {"tasks": 65, "host_jobs": 33}
        assert [task["id"] for task in payload["tasks"]] == [f"task-{index}" for index in range(65)]
        assert [job["id"] for job in payload["host_jobs"]] == [
            f"host-{index}" for index in range(33)
        ]
        assert "max_task_rows" not in payload["coverage"]
        assert "max_host_job_rows" not in payload["coverage"]

    @pytest.mark.asyncio
    async def test_preserves_host_owned_failure_attention(self, monkeypatch):
        results = ["Completed with 0 failures and no errors", "Missing credentials"]
        tasks = [
            {
                "id": f"task-{index}",
                "group": "admin",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "next_run": "2026-07-23T16:00:00+00:00",
                "last_result": result,
                "orchestration": {"state": "scheduled", "error": None},
                "run_health": {"last_status": "success", "consecutive_failures": 0},
                "health_reasons": [] if index == 0 else ["failure_shaped_result"],
            }
            for index, result in enumerate(results)
        ]
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_tasks.ipc_service_request",
            AsyncMock(
                return_value=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "tasks": tasks,
                                "host_jobs": [],
                            }
                        ),
                    )
                ]
            ),
        )

        result = await _call_tool_over_mcp("list_tasks", {})

        assert result.structuredContent is not None
        structured_tasks = result.structuredContent["tasks"]
        assert structured_tasks[0]["health_reasons"] == []
        assert structured_tasks[1]["health_reasons"] == ["failure_shaped_result"]

    @pytest.mark.asyncio
    async def test_list_tasks_declares_its_structured_output_schema(self):
        tools = await list_tools()

        list_tasks = next(tool for tool in tools if tool.name == "list_tasks")
        assert list_tasks.outputSchema is not None
        assert list_tasks.outputSchema["required"] == [
            "schema",
            "completeness",
            "counts",
            "tasks",
            "host_jobs",
            "coverage",
        ]
        properties = list_tasks.outputSchema["properties"]
        count_properties = properties["counts"]["properties"]
        assert "maximum" not in count_properties["tasks"]
        assert "maximum" not in count_properties["host_jobs"]
        assert "maxItems" not in properties["tasks"]
        assert "maxItems" not in properties["host_jobs"]

    @pytest.mark.asyncio
    async def test_host_error_never_emits_legacy_snapshot_content(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agent_runner.agent_tools._tools_tasks.ipc_service_request",
            AsyncMock(return_value=[TextContent(type="text", text="Error: timed out")]),
        )

        snapshot = [
            {
                "id": "t1",
                "prompt": "private task prompt",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "groupFolder": "group-a",
            },
            {
                "id": "h1",
                "type": "host",
                "name": "private host name",
                "command": "private-command --secret-like-argument",
                "schedule_type": "cron",
                "schedule_value": "0 1 * * *",
                "status": "active",
            },
        ]
        tasks_file = tmp_path / "current_tasks.json"
        tasks_file.write_text(json.dumps(snapshot))
        result = await _call_tool_over_mcp("list_tasks", {})
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert isinstance(result.content[0], TextContent)
        text = result.content[0].text
        assert "private task prompt" not in text
        assert "private host name" not in text
        assert "private-command" not in text


# ---------------------------------------------------------------------------
# call_tool: pause/resume/cancel task
# ---------------------------------------------------------------------------


class TestTaskLifecycle:
    """Test pause, resume, and cancel task tools."""

    @pytest.mark.asyncio
    @pytest.mark.action("task.pause")
    async def test_pause_task(self, tmp_path):

        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False, group_folder="test")):
            result = await call_tool("pause_task", {"task_id": "task-123"})
        assert "pause" in result[0].text.lower()
        files = list((tmp_path / "requests").glob("*.json"))
        envelope, payload = _read_request_file(files[0])
        assert envelope["kind"] == "pause_task"
        assert payload["taskId"] == "task-123"

    @pytest.mark.asyncio
    @pytest.mark.action("task.resume")
    async def test_resume_task(self, tmp_path):

        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False, group_folder="test")):
            result = await call_tool("resume_task", {"task_id": "task-123"})
        assert "resume" in result[0].text.lower()

    @pytest.mark.asyncio
    @pytest.mark.action("task.cancel")
    async def test_cancel_task(self, tmp_path):

        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False, group_folder="test")):
            result = await call_tool("cancel_task", {"task_id": "task-123"})
        assert "cancel" in result[0].text.lower()


class TestTodoTools:
    """Test list_todos and complete_todo MCP tools."""

    @pytest.mark.asyncio
    @pytest.mark.action("todo.list")
    async def test_list_todos_empty(self, tmp_path):

        result = await call_tool("list_todos", {})
        assert isinstance(result, list)
        assert "no" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_list_todos_shows_pending(self, tmp_path):

        todos_file = tmp_path / "todos.json"
        todos_file.write_text(
            json.dumps(
                [
                    {
                        "id": "abc",
                        "content": "rename x to y",
                        "done": False,
                        "created_at": "2026-01-01",
                    },
                ]
            )
        )

        result = await call_tool("list_todos", {})
        text = result[0].text
        assert "abc" in text
        assert "rename x to y" in text

    @pytest.mark.asyncio
    async def test_list_todos_hides_done_by_default(self, tmp_path):

        todos_file = tmp_path / "todos.json"
        todos_file.write_text(
            json.dumps(
                [
                    {"id": "abc", "content": "done item", "done": True, "created_at": "2026-01-01"},
                    {
                        "id": "def",
                        "content": "pending item",
                        "done": False,
                        "created_at": "2026-01-01",
                    },
                ]
            )
        )

        result = await call_tool("list_todos", {})
        text = result[0].text
        assert "def" in text
        assert "abc" not in text

    @pytest.mark.asyncio
    async def test_list_todos_include_done(self, tmp_path):

        todos_file = tmp_path / "todos.json"
        todos_file.write_text(
            json.dumps(
                [
                    {"id": "abc", "content": "done item", "done": True, "created_at": "2026-01-01"},
                    {
                        "id": "def",
                        "content": "pending item",
                        "done": False,
                        "created_at": "2026-01-01",
                    },
                ]
            )
        )

        result = await call_tool("list_todos", {"include_done": True})
        text = result[0].text
        assert "abc" in text
        assert "def" in text

    @pytest.mark.asyncio
    @pytest.mark.action("todo.complete")
    async def test_complete_todo(self, tmp_path):

        todos_file = tmp_path / "todos.json"
        todos_file.write_text(
            json.dumps(
                [
                    {
                        "id": "abc",
                        "content": "rename x to y",
                        "done": False,
                        "created_at": "2026-01-01",
                    },
                ]
            )
        )

        result = await call_tool("complete_todo", {"todo_id": "abc"})
        assert isinstance(result, list)
        assert "done" in result[0].text.lower()

        # Verify the file was updated
        updated = json.loads(todos_file.read_text(encoding="utf-8"))
        assert updated[0]["done"] is True

    @pytest.mark.asyncio
    async def test_complete_todo_not_found(self, tmp_path):

        todos_file = tmp_path / "todos.json"
        todos_file.write_text(json.dumps([]))

        result = await call_tool("complete_todo", {"todo_id": "nope"})
        assert hasattr(result, "isError")
        assert result.isError is True
        assert "not found" in result.content[0].text.lower()


# ---------------------------------------------------------------------------
# call_tool: unknown tool
# ---------------------------------------------------------------------------


class TestUnknownTool:
    """Test unknown tool name handling."""

    @pytest.mark.asyncio
    async def test_unknown_tool(self):

        result = await call_tool("nonexistent_tool", {})
        assert isinstance(result, list)
        assert "unknown" in result[0].text.lower()


# ---------------------------------------------------------------------------
# list_tools visibility
# ---------------------------------------------------------------------------


class TestListToolsVisibility:
    """Test tool list visibility based on admin/scheduled_task flags."""

    @pytest.mark.asyncio
    async def test_admin_sees_deploy(self):
        tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "deploy_changes" in tool_names

    @pytest.mark.asyncio
    async def test_non_admin_no_deploy(self, tmp_path):
        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False)):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "deploy_changes" not in tool_names

    @pytest.mark.asyncio
    async def test_admin_sees_register_group(self):
        tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "register_group" in tool_names

    @pytest.mark.asyncio
    async def test_non_admin_no_register_group(self, tmp_path):
        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False)):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "register_group" not in tool_names

    @pytest.mark.asyncio
    async def test_all_base_tools_present(self, tmp_path):
        with use_agent_tool_runtime(_runtime(tmp_path, is_admin=False)):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        for expected in [
            "send_message",
            "messaging_source_health",
            "list_tasks",
            "get_scheduled_task",
            "update_scheduled_task",
            "pause_task",
            "resume_task",
            "cancel_task",
            "sync_worktree_to_main",
            "publish_managed_feature",
            "reset_context",
            "list_todos",
            "complete_todo",
            "search_skills",
            "request_skill_access",
        ]:
            assert expected in tool_names, f"Missing base tool: {expected}"

    @pytest.mark.asyncio
    async def test_all_static_agent_tools_have_semantic_action_specs(self, tmp_path):
        """A tool registration needs an action ID before it reaches an agent."""
        with use_agent_tool_runtime(_runtime(tmp_path)):
            tool_names = {tool.name for tool in await list_tools()}

        cataloged_tools = {
            surface.name
            for spec in ACTION_SPECS
            for surface in spec.surfaces
            if surface.transport is ActionTransport.AGENT_TOOL and "{" not in surface.name
        }
        assert tool_names == cataloged_tools

    @pytest.mark.asyncio
    async def test_scheduled_task_can_publish_and_recover_from_errors(self, tmp_path):
        with use_agent_tool_runtime(_runtime(tmp_path, is_scheduled_task=True)):
            tools = await list_tools()

        assert "sync_worktree_to_main" in {tool.name for tool in tools}
