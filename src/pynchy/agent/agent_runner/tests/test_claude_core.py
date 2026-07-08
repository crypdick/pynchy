"""Focused tests for Claude SDK helper mapping in ``cores/claude.py``."""

from __future__ import annotations

from types import SimpleNamespace

from agent_runner.core import AgentCoreConfig
from agent_runner.cores import claude as claude_core
from agent_runner.cores.claude import ClaudeAgentCore


def _core(session_id: str | None = None) -> ClaudeAgentCore:
    return ClaudeAgentCore(
        AgentCoreConfig(
            cwd="/tmp",
            session_id=session_id,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
        )
    )


class _ThinkingBlock:
    def __init__(self, thinking: str):
        self.thinking = thinking


class _ToolUseBlock:
    def __init__(self, name: str, tool_input):
        self.name = name
        self.input = tool_input


class _ToolResultBlock:
    def __init__(self, tool_use_id: str, content, is_error: bool = False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


def test_system_event_updates_session_id():
    core = _core()
    message = SimpleNamespace(subtype="init", data={"session_id": "sid-123"})

    event = core._system_event(message)

    assert event.type == "system"
    assert event.data["system_subtype"] == "init"
    assert core.session_id == "sid-123"


def test_assistant_events_map_all_supported_block_types(monkeypatch):
    monkeypatch.setattr(claude_core, "ThinkingBlock", _ThinkingBlock)
    monkeypatch.setattr(claude_core, "ToolUseBlock", _ToolUseBlock)
    monkeypatch.setattr(claude_core, "ToolResultBlock", _ToolResultBlock)
    monkeypatch.setattr(claude_core, "TextBlock", _TextBlock)

    message = SimpleNamespace(
        content=[
            _ThinkingBlock("hmm"),
            _ToolUseBlock("Bash", {"command": "ls"}),
            _ToolResultBlock("t1", ["ok", 2], is_error=True),
            _TextBlock("done"),
        ]
    )

    events = _core()._assistant_events(message)

    assert [event.type for event in events] == ["thinking", "tool_use", "tool_result", "text"]
    assert events[0].data["thinking"] == "hmm"
    assert events[1].data["tool_name"] == "Bash"
    assert events[2].data["tool_result_content"] == '["ok", 2]'
    assert events[2].data["tool_result_is_error"] is True
    assert events[3].data["text"] == "done"


def test_result_event_updates_session_and_metadata():
    core = _core()
    message = SimpleNamespace(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=2,
        session_id="sid-r",
        total_cost_usd=0.01,
        usage={"input_tokens": 10},
        result="all done",
    )

    event = core._result_event(message)

    assert event.type == "result"
    assert event.data["result"] == "all done"
    assert event.data["result_metadata"]["session_id"] == "sid-r"
    assert event.data["result_metadata"]["usage"] == {"input_tokens": 10}
    assert core.session_id == "sid-r"
