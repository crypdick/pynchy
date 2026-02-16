"""Tests for container/agent_runner/src/agent_runner/agent_tools/.

Tests IPC file writing, schedule validation, tool authorization, and
task listing logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "container" / "agent_runner" / "src"))

from agent_runner.agent_tools._ipc import write_ipc_file

# ---------------------------------------------------------------------------
# write_ipc_file
# ---------------------------------------------------------------------------


class TestWriteIpcFile:
    """Test atomic IPC file writing."""

    def test_creates_directory(self, tmp_path):
        target = tmp_path / "subdir"
        write_ipc_file(target, {"type": "test"})
        assert target.exists()
        files = list(target.glob("*.json"))
        assert len(files) == 1

    def test_file_content_valid_json(self, tmp_path):
        data = {"type": "message", "text": "hello"}
        write_ipc_file(tmp_path, data)
        files = list(tmp_path.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content == data

    def test_filename_format(self, tmp_path):
        filename = write_ipc_file(tmp_path, {"type": "test"})
        assert filename.endswith(".json")
        # Format: {timestamp_ms}-{random_hex}.json
        parts = filename.replace(".json", "").split("-")
        assert len(parts) >= 2
        # First part should be a timestamp (numeric)
        assert parts[0].isdigit()

    def test_no_temp_files_left(self, tmp_path):
        write_ipc_file(tmp_path, {"type": "test"})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_multiple_writes_unique_filenames(self, tmp_path):
        f1 = write_ipc_file(tmp_path, {"n": 1})
        f2 = write_ipc_file(tmp_path, {"n": 2})
        assert f1 != f2
        assert len(list(tmp_path.glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# call_tool: schedule_task validation
# ---------------------------------------------------------------------------


class TestScheduleTaskValidation:
    """Test schedule_task input validation via call_tool.

    These tests exercise the validation logic in the call_tool handler
    by calling it directly with mocked environment state.
    """

    @pytest.fixture(autouse=True)
    def _patch_env(self, tmp_path):
        """Patch module-level state for testing."""
        with (
            patch("agent_runner.agent_tools._ipc.chat_jid", "test@g.us"),
            patch("agent_runner.agent_tools._ipc.group_folder", "test-group"),
            patch("agent_runner.agent_tools._ipc.is_god", True),
            patch("agent_runner.agent_tools._ipc.is_scheduled_task", False),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
            yield

    @pytest.mark.asyncio
    async def test_valid_cron(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"):
            result = await call_tool(
                "schedule_task",
                {
                    "prompt": "do something",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                },
            )
        # Should succeed (list of TextContent, not CallToolResult with isError)
        assert isinstance(result, list)
        assert "scheduled" in result[0].text.lower() or "Task" in result[0].text

    @pytest.mark.asyncio
    async def test_invalid_cron_returns_error(self):
        from agent_runner.agent_tools._server import call_tool

        result = await call_tool(
            "schedule_task",
            {
                "prompt": "do something",
                "schedule_type": "cron",
                "schedule_value": "not a cron",
            },
        )
        # Should be a CallToolResult with isError=True
        assert hasattr(result, "isError")
        assert result.isError is True
        assert "Invalid cron" in result.content[0].text

    @pytest.mark.asyncio
    async def test_valid_interval(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"):
            result = await call_tool(
                "schedule_task",
                {
                    "prompt": "repeat",
                    "schedule_type": "interval",
                    "schedule_value": "300000",
                },
            )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_invalid_interval_negative(self):
        from agent_runner.agent_tools._server import call_tool

        result = await call_tool(
            "schedule_task",
            {
                "prompt": "repeat",
                "schedule_type": "interval",
                "schedule_value": "-100",
            },
        )
        assert hasattr(result, "isError")
        assert result.isError is True
        assert "Invalid interval" in result.content[0].text

    @pytest.mark.asyncio
    async def test_invalid_interval_non_numeric(self):
        from agent_runner.agent_tools._server import call_tool

        result = await call_tool(
            "schedule_task",
            {
                "prompt": "repeat",
                "schedule_type": "interval",
                "schedule_value": "not-a-number",
            },
        )
        assert result.isError is True

    @pytest.mark.asyncio
    async def test_invalid_interval_zero(self):
        from agent_runner.agent_tools._server import call_tool

        result = await call_tool(
            "schedule_task",
            {
                "prompt": "repeat",
                "schedule_type": "interval",
                "schedule_value": "0",
            },
        )
        assert result.isError is True

    @pytest.mark.asyncio
    async def test_valid_once(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"):
            result = await call_tool(
                "schedule_task",
                {
                    "prompt": "one-time",
                    "schedule_type": "once",
                    "schedule_value": "2026-03-01T10:00:00",
                },
            )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_invalid_once_timestamp(self):
        from agent_runner.agent_tools._server import call_tool

        result = await call_tool(
            "schedule_task",
            {
                "prompt": "one-time",
                "schedule_type": "once",
                "schedule_value": "not-a-timestamp",
            },
        )
        assert result.isError is True
        assert "Invalid timestamp" in result.content[0].text

    @pytest.mark.asyncio
    async def test_non_god_cannot_set_target_group(self, tmp_path):
        """Non-god groups should have target_group_jid ignored."""
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
            result = await call_tool(
                "schedule_task",
                {
                    "prompt": "task",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                    "target_group_jid": "other@g.us",
                },
            )
        assert isinstance(result, list)
        # Verify the IPC file uses the caller's JID, not the target
        files = list((tmp_path / "tasks").glob("*.json"))
        data = json.loads(files[0].read_text())
        assert data["targetJid"] == "test@g.us"

    @pytest.mark.asyncio
    async def test_god_can_set_target_group(self, tmp_path):
        """God groups should be able to set target_group_jid."""
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.is_god", True),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
            result = await call_tool(
                "schedule_task",
                {
                    "prompt": "task",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                    "target_group_jid": "other@g.us",
                },
            )
        assert isinstance(result, list)
        files = list((tmp_path / "tasks").glob("*.json"))
        data = json.loads(files[0].read_text())
        assert data["targetJid"] == "other@g.us"


# ---------------------------------------------------------------------------
# call_tool: register_group authorization
# ---------------------------------------------------------------------------


class TestRegisterGroupAuth:
    """Test register_group god-only authorization."""

    @pytest.mark.asyncio
    async def test_non_god_register_group_rejected(self):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.group_folder", "non-god"),
        ):
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
        assert "god" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_god_register_group_accepted(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.is_god", True),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
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
    """Test deploy_changes god-only authorization."""

    @pytest.mark.asyncio
    async def test_non_god_deploy_rejected(self):
        from agent_runner.agent_tools._server import call_tool

        with patch("agent_runner.agent_tools._ipc.is_god", False):
            result = await call_tool("deploy_changes", {})
        assert hasattr(result, "isError")
        assert result.isError is True
        assert "god" in result.content[0].text.lower()


# ---------------------------------------------------------------------------
# call_tool: send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    """Test send_message tool."""

    @pytest.mark.asyncio
    async def test_basic_send(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.chat_jid", "test@g.us"),
            patch("agent_runner.agent_tools._ipc.group_folder", "test"),
            patch("agent_runner.agent_tools._ipc.MESSAGES_DIR", tmp_path / "messages"),
        ):
            result = await call_tool("send_message", {"text": "Hello world"})

        assert isinstance(result, list)
        assert "sent" in result[0].text.lower()

        files = list((tmp_path / "messages").glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["text"] == "Hello world"
        assert data["chatJid"] == "test@g.us"
        assert data["type"] == "message"

    @pytest.mark.asyncio
    async def test_send_with_sender(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.chat_jid", "test@g.us"),
            patch("agent_runner.agent_tools._ipc.group_folder", "test"),
            patch("agent_runner.agent_tools._ipc.MESSAGES_DIR", tmp_path / "messages"),
        ):
            await call_tool("send_message", {"text": "Update", "sender": "Researcher"})

        files = list((tmp_path / "messages").glob("*.json"))
        data = json.loads(files[0].read_text())
        assert data["sender"] == "Researcher"


# ---------------------------------------------------------------------------
# call_tool: list_tasks
# ---------------------------------------------------------------------------


class TestListTasks:
    """Test list_tasks tool behavior."""

    @pytest.mark.asyncio
    async def test_no_tasks_file(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with patch("agent_runner.agent_tools._ipc.IPC_DIR", tmp_path):
            result = await call_tool("list_tasks", {})
        assert isinstance(result, list)
        assert "no" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_empty_task_list(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        tasks_file = tmp_path / "current_tasks.json"
        tasks_file.write_text("[]")
        with (
            patch("agent_runner.agent_tools._ipc.IPC_DIR", tmp_path),
            patch("agent_runner.agent_tools._ipc.is_god", True),
        ):
            result = await call_tool("list_tasks", {})
        assert "no" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_god_sees_all_tasks(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        tasks = [
            {
                "id": "t1",
                "prompt": "Task one description here for testing",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "groupFolder": "group-a",
            },
            {
                "id": "t2",
                "prompt": "Task two description here for testing",
                "schedule_type": "interval",
                "schedule_value": "300000",
                "status": "active",
                "groupFolder": "group-b",
            },
        ]
        tasks_file = tmp_path / "current_tasks.json"
        tasks_file.write_text(json.dumps(tasks))
        with (
            patch("agent_runner.agent_tools._ipc.IPC_DIR", tmp_path),
            patch("agent_runner.agent_tools._ipc.is_god", True),
        ):
            result = await call_tool("list_tasks", {})
        text = result[0].text
        assert "t1" in text
        assert "t2" in text

    @pytest.mark.asyncio
    async def test_non_god_sees_own_tasks_only(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        tasks = [
            {
                "id": "t1",
                "prompt": "My task description here for testing",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "groupFolder": "my-group",
            },
            {
                "id": "t2",
                "prompt": "Other task description here for testing",
                "schedule_type": "cron",
                "schedule_value": "0 10 * * *",
                "status": "active",
                "groupFolder": "other-group",
            },
        ]
        tasks_file = tmp_path / "current_tasks.json"
        tasks_file.write_text(json.dumps(tasks))
        with (
            patch("agent_runner.agent_tools._ipc.IPC_DIR", tmp_path),
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.group_folder", "my-group"),
        ):
            result = await call_tool("list_tasks", {})
        text = result[0].text
        assert "t1" in text
        assert "t2" not in text


# ---------------------------------------------------------------------------
# call_tool: pause/resume/cancel task
# ---------------------------------------------------------------------------


class TestTaskLifecycle:
    """Test pause, resume, and cancel task tools."""

    @pytest.mark.asyncio
    async def test_pause_task(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.group_folder", "test"),
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
            result = await call_tool("pause_task", {"task_id": "task-123"})
        assert "pause" in result[0].text.lower()
        files = list((tmp_path / "tasks").glob("*.json"))
        data = json.loads(files[0].read_text())
        assert data["type"] == "pause_task"
        assert data["taskId"] == "task-123"

    @pytest.mark.asyncio
    async def test_resume_task(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.group_folder", "test"),
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
            result = await call_tool("resume_task", {"task_id": "task-123"})
        assert "resume" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_cancel_task(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with (
            patch("agent_runner.agent_tools._ipc.group_folder", "test"),
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.TASKS_DIR", tmp_path / "tasks"),
        ):
            result = await call_tool("cancel_task", {"task_id": "task-123"})
        assert "cancel" in result[0].text.lower()


# ---------------------------------------------------------------------------
# call_tool: list_todos / complete_todo
# ---------------------------------------------------------------------------


class TestTodoTools:
    """Test list_todos and complete_todo MCP tools."""

    @pytest.mark.asyncio
    async def test_list_todos_empty(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        with patch("agent_runner.agent_tools._tools_todos._TODOS_FILE", tmp_path / "todos.json"):
            result = await call_tool("list_todos", {})
        assert isinstance(result, list)
        assert "no" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_list_todos_shows_pending(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

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

        with patch("agent_runner.agent_tools._tools_todos._TODOS_FILE", todos_file):
            result = await call_tool("list_todos", {})
        text = result[0].text
        assert "abc" in text
        assert "rename x to y" in text

    @pytest.mark.asyncio
    async def test_list_todos_hides_done_by_default(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

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

        with patch("agent_runner.agent_tools._tools_todos._TODOS_FILE", todos_file):
            result = await call_tool("list_todos", {})
        text = result[0].text
        assert "def" in text
        assert "abc" not in text

    @pytest.mark.asyncio
    async def test_list_todos_include_done(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

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

        with patch("agent_runner.agent_tools._tools_todos._TODOS_FILE", todos_file):
            result = await call_tool("list_todos", {"include_done": True})
        text = result[0].text
        assert "abc" in text
        assert "def" in text

    @pytest.mark.asyncio
    async def test_complete_todo(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

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

        with patch("agent_runner.agent_tools._tools_todos._TODOS_FILE", todos_file):
            result = await call_tool("complete_todo", {"todo_id": "abc"})
        assert isinstance(result, list)
        assert "done" in result[0].text.lower()

        # Verify the file was updated
        updated = json.loads(todos_file.read_text())
        assert updated[0]["done"] is True

    @pytest.mark.asyncio
    async def test_complete_todo_not_found(self, tmp_path):
        from agent_runner.agent_tools._server import call_tool

        todos_file = tmp_path / "todos.json"
        todos_file.write_text(json.dumps([]))

        with patch("agent_runner.agent_tools._tools_todos._TODOS_FILE", todos_file):
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
        from agent_runner.agent_tools._server import call_tool

        result = await call_tool("nonexistent_tool", {})
        assert isinstance(result, list)
        assert "unknown" in result[0].text.lower()


# ---------------------------------------------------------------------------
# list_tools visibility
# ---------------------------------------------------------------------------


class TestListToolsVisibility:
    """Test tool list visibility based on god/scheduled_task flags."""

    @pytest.mark.asyncio
    async def test_god_sees_deploy(self):
        from agent_runner.agent_tools._server import list_tools

        with (
            patch("agent_runner.agent_tools._ipc.is_god", True),
            patch("agent_runner.agent_tools._ipc.is_scheduled_task", False),
        ):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "deploy_changes" in tool_names

    @pytest.mark.asyncio
    async def test_non_god_no_deploy(self):
        from agent_runner.agent_tools._server import list_tools

        with (
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.is_scheduled_task", False),
        ):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "deploy_changes" not in tool_names

    @pytest.mark.asyncio
    async def test_scheduled_task_sees_finished_work(self):
        from agent_runner.agent_tools._server import list_tools

        with (
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.is_scheduled_task", True),
        ):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "finished_work" in tool_names

    @pytest.mark.asyncio
    async def test_non_scheduled_no_finished_work(self):
        from agent_runner.agent_tools._server import list_tools

        with (
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.is_scheduled_task", False),
        ):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        assert "finished_work" not in tool_names

    @pytest.mark.asyncio
    async def test_all_base_tools_present(self):
        from agent_runner.agent_tools._server import list_tools

        with (
            patch("agent_runner.agent_tools._ipc.is_god", False),
            patch("agent_runner.agent_tools._ipc.is_scheduled_task", False),
        ):
            tools = await list_tools()
        tool_names = [t.name for t in tools]
        for expected in [
            "send_message",
            "schedule_task",
            "list_tasks",
            "pause_task",
            "resume_task",
            "cancel_task",
            "register_group",
            "sync_worktree_to_main",
            "reset_context",
            "list_todos",
            "complete_todo",
        ]:
            assert expected in tool_names, f"Missing base tool: {expected}"
