"""Public plugin-hook loading and built-in security roster behavior."""

from __future__ import annotations

import sys
from pathlib import Path

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
