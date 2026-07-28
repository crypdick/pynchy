"""Regression tests for the shared Claude tool roster (cores/tools.py).

Both Claude cores (SDK and claude-cli) draw their built-in tool menu from the
same constants, so parity holds by construction. These tests pin the one policy
decision that is easy to silently undo: native Teams tools must stay OUT of the
allow-list, because pynchy resumes a single shared session JSONL by bare
session-id and a teammate branching that transcript mid-turn could strand the
next resume on a stale tip.
"""

from __future__ import annotations

import pytest

from agent_runner.cores import BUILTIN_ALLOWED_TOOLS, DISALLOWED_TOOLS

# Native multi-agent tools that let the claude binary spawn transcript-writing
# child processes. Unsafe until teammates get per-teammate session isolation.
_TEAMS_TOOLS = {"TeamCreate", "TeamDelete", "SendMessage"}


def test_teams_tools_are_not_allow_listed():
    assert _TEAMS_TOOLS.isdisjoint(BUILTIN_ALLOWED_TOOLS)


def test_task_tools_remain_allow_listed():
    # Task sidechains write off the main chain and resume safely, so they stay.
    for tool in ("Task", "TaskOutput", "TaskStop"):
        assert tool in BUILTIN_ALLOWED_TOOLS


def test_pynchy_messaging_goes_through_mcp_not_native_send_message():
    # Agent->human messaging is the namespaced MCP tool, never native SendMessage.
    assert "mcp__pynchy__*" in BUILTIN_ALLOWED_TOOLS
    assert "SendMessage" not in BUILTIN_ALLOWED_TOOLS


def test_interactive_tools_are_disallowed():
    # Plan-mode / question tools block a headless container on approval.
    assert set(DISALLOWED_TOOLS) == {"AskUserQuestion", "EnterPlanMode", "ExitPlanMode"}


def test_both_cores_draw_from_the_shared_roster():
    # Parity by construction: both core modules import the same constant object.
    pytest.importorskip("claude_agent_sdk")
    from agent_runner.cores import (  # noqa: PLC0415 - optional SDK import.
        claude,
        claude_cli,
    )

    assert claude.BUILTIN_ALLOWED_TOOLS is BUILTIN_ALLOWED_TOOLS
    assert claude_cli.BUILTIN_ALLOWED_TOOLS is BUILTIN_ALLOWED_TOOLS
    assert claude.DISALLOWED_TOOLS is DISALLOWED_TOOLS
    assert claude_cli.DISALLOWED_TOOLS is DISALLOWED_TOOLS
