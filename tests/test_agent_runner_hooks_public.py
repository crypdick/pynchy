"""Public plugin-hook loading and built-in security roster behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.hooks import (
    HookDecision,
    HookEvent,
    before_tool_use_roster,
    builtin_before_tool_hooks,
    builtin_security_hook,
    load_hooks,
)


def test_load_hooks_registers_event_handlers_and_skips_empty_specs(tmp_path: Path, capsys) -> None:
    hook_file = tmp_path / "audit.py"
    hook_file.write_text(
        "def before_tool_use(tool_name, tool_input):\n"
        "    return {'allowed': True}\n"
        "def session_start(*args):\n"
        "    return None\n"
    )

    hooks = load_hooks(
        [
            {"name": "audit", "module_path": str(hook_file)},
            {"name": "missing-path"},
        ]
    )

    assert len(hooks[HookEvent.BEFORE_TOOL_USE]) == 1
    assert len(hooks[HookEvent.SESSION_START]) == 1
    assert "Loaded hook 'audit'" in capsys.readouterr().err


def test_load_hooks_reports_empty_and_broken_modules(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty.py"
    empty.write_text("VALUE = 1\n")
    broken = tmp_path / "broken.py"
    broken.write_text("raise RuntimeError('load failed')\n")

    hooks = load_hooks(
        [
            {"name": "empty", "module_path": str(empty)},
            {"name": "broken", "module_path": str(broken)},
        ]
    )

    assert all(not functions for functions in hooks.values())
    diagnostics = capsys.readouterr().err
    assert "has no event handlers" in diagnostics
    assert "Failed to load hook 'broken'" in diagnostics


def test_before_tool_roster_places_builtin_gate_before_plugin_gates() -> None:
    def plugin_gate(_name: str, _input: dict[str, object]) -> HookDecision:
        return HookDecision(allowed=True)

    roster = before_tool_use_roster({HookEvent.BEFORE_TOOL_USE: [plugin_gate]})

    assert roster[0] is builtin_security_hook
    assert roster[1] is plugin_gate
    assert builtin_before_tool_hooks() == [builtin_security_hook]


@pytest.mark.asyncio
async def test_builtin_security_hook_allows_when_all_policies_allow() -> None:
    allow = AsyncMock(return_value=HookDecision(allowed=True))
    with (
        patch("agent_runner.security.artifact_gate.artifact_security_hook", allow),
        patch("agent_runner.security.bash_gate.bash_security_hook", allow),
        patch("agent_runner.security.guard_git.guard_git_hook", allow),
    ):
        decision = await builtin_security_hook("read_file", {"path": "README.md"})

    assert decision == HookDecision(allowed=True)
    assert allow.await_count == 3


@pytest.mark.asyncio
async def test_builtin_security_hook_stops_at_first_denial() -> None:
    denied = AsyncMock(return_value=HookDecision(allowed=False, reason="blocked"))
    later = AsyncMock(return_value=HookDecision(allowed=True))
    with (
        patch("agent_runner.security.artifact_gate.artifact_security_hook", denied),
        patch("agent_runner.security.bash_gate.bash_security_hook", later),
        patch("agent_runner.security.guard_git.guard_git_hook", later),
    ):
        decision = await builtin_security_hook("run_command", {"command": "unsafe"})

    assert decision == HookDecision(allowed=False, reason="blocked")
    denied.assert_awaited_once()
    later.assert_not_awaited()
