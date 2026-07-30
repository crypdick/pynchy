"""Public tool-parsing contract tests for OpenAI Responses item shapes."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent_runner.cores.openai import extract_tool_call, extract_tool_result


@dataclass(frozen=True)
class _ToolOutputItem:
    raw_item: dict[str, object]
    output: str


@pytest.mark.parametrize(
    ("raw", "expected_name", "expected_input"),
    [
        (
            {
                "type": "shell_call",
                "action": {"commands": ["git status"], "timeout_ms": 5_000},
            },
            "shell",
            {"commands": ["git status"], "timeout_ms": 5_000},
        ),
        (
            {"type": "local_shell_call", "action": {"command": ["pwd"]}},
            "shell",
            {"command": ["pwd"], "commands": ["pwd"]},
        ),
        (
            {
                "type": "function_call",
                "name": "lookup_issue",
                "arguments": '{"issue_id":"ENG-1"}',
            },
            "lookup_issue",
            {"issue_id": "ENG-1"},
        ),
        (
            {
                "type": "mcp_call",
                "name": "search_notes",
                "arguments": {"query": "tracing"},
            },
            "search_notes",
            {"query": "tracing"},
        ),
        (
            {
                "type": "apply_patch_call",
                "operation": {
                    "type": "update_file",
                    "path": "src/app.py",
                    "diff": "@@ -1 +1 @@",
                },
            },
            "apply_patch",
            {
                "type": "update_file",
                "path": "src/app.py",
                "diff": "@@ -1 +1 @@",
            },
        ),
    ],
)
def test_extract_tool_call_preserves_responses_tool_shapes(
    raw: dict[str, object],
    expected_name: str,
    expected_input: dict[str, object],
) -> None:
    tool_name, tool_input = extract_tool_call(raw)

    assert tool_name == expected_name
    assert tool_input == expected_input


@pytest.mark.parametrize(
    ("raw_item", "is_error"),
    [
        (
            {
                "type": "shell_call_output",
                "call_id": "shell-success",
                "status": "completed",
                "output": [{"outcome": {"type": "exit", "exit_code": 0}}],
            },
            False,
        ),
        (
            {
                "type": "shell_call_output",
                "call_id": "shell-failed",
                "status": "completed",
                "output": [{"outcome": {"type": "exit", "exit_code": 7}}],
            },
            True,
        ),
        (
            {
                "type": "shell_call_output",
                "call_id": "shell-timeout",
                "status": "completed",
                "output": [{"outcome": {"type": "timeout"}}],
            },
            True,
        ),
        (
            {
                "type": "apply_patch_call_output",
                "call_id": "patch-failed",
                "status": "failed",
            },
            True,
        ),
    ],
)
def test_extract_tool_result_preserves_structured_failure_state(
    raw_item: dict[str, object],
    is_error: bool,
) -> None:
    tool_result_id, output, result_is_error = extract_tool_result(
        _ToolOutputItem(raw_item=raw_item, output="tool output")
    )

    assert tool_result_id == raw_item["call_id"]
    assert output == "tool output"
    assert result_is_error is is_error
