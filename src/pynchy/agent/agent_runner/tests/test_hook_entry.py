"""Tests for the shared CLI ``PreToolUse`` subprocess entrypoint.

Claude Code and Codex run the security gate as a fresh subprocess per tool call
(security/hook_entry.py). These tests pin the invariant that this subprocess
enforces the *same* BEFORE_TOOL_USE roster as SDK cores -- built-in hooks plus
any plugin hooks handed over via the ``PYNCHY_PLUGIN_HOOKS`` env var -- so the
gate can never silently differ by which core is selected.
"""

from __future__ import annotations

import io
import json

import pytest

from agent_runner.security import hook_entry


def _write_plugin(tmp_path, *, deny_tool: str, name: str = "denier") -> dict[str, str]:
    """Write a plugin module whose before_tool_use denies one tool; return its spec."""
    path = tmp_path / f"{name}.py"
    path.write_text(
        "from agent_runner.hooks import HookDecision\n"
        "\n"
        "async def before_tool_use(tool_name, tool_input):\n"
        f"    if tool_name == {deny_tool!r}:\n"
        f"        return HookDecision(allowed=False, reason='plugin denied {deny_tool}')\n"
        "    return HookDecision(allowed=True)\n"
    )
    return {"name": name, "module_path": str(path)}


# ---------------------------------------------------------------------------
# main(): end-to-end deny/allow via the forwarded plugin gate
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, capsys, *, payload: dict[str, object]) -> str:
    monkeypatch.setattr(
        hook_entry.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    with pytest.raises(SystemExit) as exc:
        hook_entry.main()
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_main_enforces_plugin_deny(monkeypatch, capsys, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Read")
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    out = _run_main(monkeypatch, capsys, payload={"tool_name": "Read", "tool_input": {}})
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "plugin denied Read" in decision["permissionDecisionReason"]


def test_main_allows_tool_the_plugin_permits(monkeypatch, capsys, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Write")  # denies Write, not Read
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    # Read passes the plugin gate and both builtins (non-Bash -> allow).
    assert not _run_main(monkeypatch, capsys, payload={"tool_name": "Read", "tool_input": {}})


def test_main_accepts_codex_camel_case_payload(monkeypatch, capsys, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Read")
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    out = _run_main(
        monkeypatch,
        capsys,
        payload={"toolName": "Read", "toolInput": {"file_path": "/workspace/README.md"}},
    )

    assert "plugin denied Read" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_main_accepts_codex_nested_tool_payload(monkeypatch, capsys, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Read")
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    out = _run_main(
        monkeypatch,
        capsys,
        payload={"tool": {"name": "Read", "input": {"file_path": "/workspace/README.md"}}},
    )

    assert "plugin denied Read" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
