"""Focused tests for Claude SDK helper mapping in ``cores/claude.py``."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import SystemMessage

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.claude import ClaudeAgentCore
from agent_runner.cores.claude_messages import (
    ClaudeAssistantEvent,
    ClaudeResultEvent,
    ClaudeSystemEvent,
    ClaudeTextBlock,
    ClaudeThinkingBlock,
    ClaudeToolResultBlock,
    ClaudeToolUseBlock,
    parse_claude_sdk_event,
)


def _core(session_id: str | None = None) -> ClaudeAgentCore:
    return ClaudeAgentCore(
        AgentCoreConfig(
            cwd=str(Path.cwd()),
            session_id=session_id,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
        )
    )


def test_system_event_updates_session_id():
    core = _core()
    message = ClaudeSystemEvent(subtype="init", data={"session_id": "sid-123"})

    event = core._system_event(message)

    assert event.type == "system"
    assert event.data["system_subtype"] == "init"
    assert core.session_id == "sid-123"


def test_sdk_system_message_is_parsed_at_the_core_boundary():
    parsed = parse_claude_sdk_event(SystemMessage("init", {"session_id": "sid-123"}))

    assert parsed == ClaudeSystemEvent(subtype="init", data={"session_id": "sid-123"})


def test_assistant_events_map_all_supported_block_types():
    message = ClaudeAssistantEvent(
        content=(
            ClaudeThinkingBlock("hmm"),
            ClaudeToolUseBlock("Bash", {"command": "ls"}),
            ClaudeToolResultBlock("t1", ["ok", 2], is_error=True),
            ClaudeTextBlock("done"),
        )
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
    message = ClaudeResultEvent(
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
