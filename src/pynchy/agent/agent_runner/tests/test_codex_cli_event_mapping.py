"""Codex CLI JSONL item-to-agent-event contract tests."""

from __future__ import annotations

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.codex import CodexCLIAgentCore


def _core() -> CodexCLIAgentCore:
    return CodexCLIAgentCore(
        AgentCoreConfig(
            cwd="/workspace/repos/owner/project",
            session_id=None,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
            mcp_servers={},
            extra={"model": "gpt-5.2-codex"},
        )
    )


def test_stream_event_maps_agent_message_to_text() -> None:
    events = _core().map_stream_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
    )

    assert [event.type for event in events] == ["text"]
    assert events[0].data["text"] == "done"


def test_stream_event_maps_command_item_to_tool_events() -> None:
    core = _core()
    started = core.map_stream_event(
        {
            "type": "item.started",
            "item": {"id": "cmd-1", "type": "command_execution", "command": "ls -la"},
        }
    )
    completed = core.map_stream_event(
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "ok",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )

    assert started[0].data == {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
    assert completed[0].data == {
        "tool_result_id": "cmd-1",
        "tool_result_content": "ok",
        "tool_result_is_error": False,
    }


def test_stream_event_maps_failed_command_status_to_error_result() -> None:
    (completed,) = _core().map_stream_event(
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-2",
                "type": "command_execution",
                "command": "false",
                "aggregated_output": "failed",
                "exit_code": 1,
                "status": "failed",
            },
        }
    )

    assert completed.data["tool_result_content"] == "failed"
    assert completed.data["tool_result_is_error"] is True


def test_stream_event_maps_mcp_tool_name_arguments_and_result() -> None:
    core = _core()
    started = core.map_stream_event(
        {
            "type": "item.started",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "calendar",
                "tool": "list_events",
                "arguments": {"days": 2},
                "status": "in_progress",
            },
        }
    )
    completed = core.map_stream_event(
        {
            "type": "item.completed",
            "item": {
                "id": "mcp-1",
                "type": "mcp_tool_call",
                "server": "calendar",
                "tool": "list_events",
                "arguments": {"days": 2},
                "result": {
                    "content": [{"type": "text", "text": "two events"}],
                    "structured_content": None,
                },
                "status": "completed",
            },
        }
    )

    assert started[0].data == {
        "tool_name": "list_events",
        "tool_input": {"server": "calendar", "days": 2},
    }
    assert completed[0].data["tool_result_content"] == "two events"
    assert completed[0].data["tool_result_is_error"] is False


def test_stream_event_maps_mcp_error_message() -> None:
    (completed,) = _core().map_stream_event(
        {
            "type": "item.completed",
            "item": {
                "id": "mcp-2",
                "type": "mcp_tool_call",
                "server": "calendar",
                "tool": "list_events",
                "arguments": {},
                "error": {"message": "provider unavailable"},
                "status": "failed",
            },
        }
    )

    assert completed.data["tool_result_content"] == "provider unavailable"
    assert completed.data["tool_result_is_error"] is True


def test_stream_event_maps_completed_file_change_to_tool_pair() -> None:
    events = _core().map_stream_event(
        {
            "type": "item.completed",
            "item": {
                "id": "patch-1",
                "type": "file_change",
                "changes": [{"path": "src/app.py", "kind": "update"}],
                "status": "completed",
            },
        }
    )

    assert [event.type for event in events] == ["tool_use", "tool_result"]
    assert events[0].data == {
        "tool_name": "apply_patch",
        "tool_input": {"changes": [{"path": "src/app.py", "kind": "update"}]},
    }
    assert events[1].data["tool_result_id"] == "patch-1"
    assert '"status": "completed"' in events[1].data["tool_result_content"]
    assert events[1].data["tool_result_is_error"] is False
