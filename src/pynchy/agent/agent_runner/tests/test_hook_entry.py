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

from agent_runner.hooks import before_tool_use_roster, builtin_before_tool_hooks, load_hooks
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


def _qualnames(hooks) -> list[str]:
    return [h.__qualname__ for h in hooks]


# ---------------------------------------------------------------------------
# _load_roster: composes builtin + plugin from the env var
# ---------------------------------------------------------------------------


def test_load_roster_builtins_only_without_env(monkeypatch):
    monkeypatch.delenv("PYNCHY_PLUGIN_HOOKS", raising=False)
    assert _qualnames(hook_entry._load_roster()) == _qualnames(builtin_before_tool_hooks())


def test_load_roster_includes_plugin_hooks(monkeypatch, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Read")
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    roster = hook_entry._load_roster()
    assert len(roster) == len(builtin_before_tool_hooks()) + 1
    assert roster[-1].__qualname__ == "before_tool_use"


def test_load_roster_matches_sdk_core_composition(monkeypatch, tmp_path):
    """Parity by construction: the subprocess roster == what the SDK core builds."""
    spec = _write_plugin(tmp_path, deny_tool="Read")
    specs = [spec]
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps(specs))

    # What the SDK/OpenAI cores compute (cores/claude.py, cores/openai.py):
    sdk_roster = before_tool_use_roster(load_hooks(specs))
    # What the CLI subprocess computes from the forwarded env:
    cli_roster = hook_entry._load_roster()

    assert _qualnames(cli_roster) == _qualnames(sdk_roster)


def test_load_roster_malformed_env_falls_back_to_builtins(monkeypatch):
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", "{not json")
    assert _qualnames(hook_entry._load_roster()) == _qualnames(builtin_before_tool_hooks())


# ---------------------------------------------------------------------------
# main(): end-to-end deny/allow via the forwarded plugin gate
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, capsys, *, tool_name: str) -> str:
    monkeypatch.setattr(
        hook_entry.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": tool_name, "tool_input": {}})),
    )
    with pytest.raises(SystemExit) as exc:
        hook_entry.main()
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_main_enforces_plugin_deny(monkeypatch, capsys, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Read")
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    out = _run_main(monkeypatch, capsys, tool_name="Read")
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "plugin denied Read" in decision["permissionDecisionReason"]


def test_main_allows_tool_the_plugin_permits(monkeypatch, capsys, tmp_path):
    spec = _write_plugin(tmp_path, deny_tool="Write")  # denies Write, not Read
    monkeypatch.setenv("PYNCHY_PLUGIN_HOOKS", json.dumps([spec]))

    # Read passes the plugin gate and both builtins (non-Bash -> allow).
    assert _run_main(monkeypatch, capsys, tool_name="Read") == ""


def test_extract_tool_call_accepts_codex_camel_case_payload():
    payload = {"toolName": "Bash", "toolInput": {"command": "git status"}}

    assert hook_entry._extract_tool_call(payload) == ("Bash", {"command": "git status"})


def test_extract_tool_call_accepts_codex_nested_tool_payload():
    payload = {"tool": {"name": "Bash", "input": {"command": "git diff"}}}

    assert hook_entry._extract_tool_call(payload) == ("Bash", {"command": "git diff"})
