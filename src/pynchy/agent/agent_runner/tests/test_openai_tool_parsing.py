"""Public tool-parsing contract tests for OpenAI Responses item shapes."""

from __future__ import annotations

import pytest

from agent_runner.cores.openai import extract_tool_call


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
