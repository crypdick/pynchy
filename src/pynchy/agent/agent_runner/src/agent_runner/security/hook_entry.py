"""CLI ``PreToolUse`` hook entrypoint for CLI-backed agent cores.

Claude Code and Codex run ``PreToolUse`` hooks as shell commands: they pass the
tool call as JSON on the command's **stdin** and read a decision JSON from the
command's **stdout**. This module bridges that protocol back to pynchy's
in-Python BEFORE_TOOL_USE security functions (``bash_security_hook``,
``guard_git_hook``), so CLI cores enforce the exact same gate as SDK cores.

The gate itself is stateless in the container -- ``bash_security_hook``
delegates taint/Cop decisions to the host over file IPC -- so evaluating it from
this short-lived subprocess is functionally identical to in-process evaluation.
Invoked as ``python -m agent_runner.security.hook_entry``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from agent_runner.hooks import (
    BeforeToolUseHook,
    HookDecision,
    before_tool_use_roster,
    load_hooks,
)

# Env var CLI-backed cores use to hand plugin BEFORE_TOOL_USE specs to this
# subprocess. See _load_roster.
_PLUGIN_HOOKS_ENV = "PYNCHY_PLUGIN_HOOKS"


def _log(message: str) -> None:
    sys.stderr.write(f"[cli-hook] {message}\n")
    sys.stderr.flush()


def _load_roster() -> tuple[BeforeToolUseHook, ...]:
    """Compose the exact BEFORE_TOOL_USE gate the SDK core enforces.

    This subprocess is spawned fresh by the ``claude`` binary per tool call, so
    it can't share the core's in-memory config. The claude-cli core forwards the
    plugin-hook specs via ``PYNCHY_PLUGIN_HOOKS``; we load them and run them
    through the shared before_tool_use_roster (builtin hooks first, then plugin
    hooks). Because both cores compose here, the CLI subprocess can never enforce
    a different set than the SDK core -- parity by construction, not by comment.
    """
    raw = os.environ.get(_PLUGIN_HOOKS_ENV, "")
    specs: list[dict[str, str]]
    try:
        parsed = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        _log(f"unparseable {_PLUGIN_HOOKS_ENV}; running built-in gate only")
        specs = []
    else:
        specs = []
        malformed = not isinstance(parsed, list)
        if isinstance(parsed, list):
            for spec in parsed:
                if not isinstance(spec, dict):
                    malformed = True
                    break
                name = spec.get("name")
                module_path = spec.get("module_path")
                if not isinstance(name, str) or not isinstance(module_path, str):
                    malformed = True
                    break
                specs.append({"name": name, "module_path": module_path})
        if malformed:
            _log(f"malformed {_PLUGIN_HOOKS_ENV}; running built-in gate only")
            specs = []
    return tuple(before_tool_use_roster(load_hooks(specs)))


async def _evaluate(
    hooks: tuple[BeforeToolUseHook, ...], tool_name: str, tool_input: dict[str, Any]
) -> HookDecision | None:
    """Run the gate; return the first denying decision, else None."""
    for hook in hooks:
        decision = await hook(tool_name, tool_input)
        if not decision.allowed:
            return decision
    return None


def _payload_tool(data: dict[str, Any]) -> dict[str, Any]:
    tool = data.get("tool")
    return tool if isinstance(tool, dict) else {}


def _extract_tool_name(data: dict[str, Any]) -> str:
    """Extract the tool name from either flat or nested hook payloads."""
    tool = _payload_tool(data)
    tool_name = data.get("tool_name") or data.get("toolName") or ""
    tool_name = tool_name or tool.get("name") or tool.get("toolName") or ""
    return str(tool_name)


def _extract_tool_input(data: dict[str, Any]) -> dict[str, Any]:
    """Extract and normalize the tool input payload."""
    tool = _payload_tool(data)
    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    tool_input = tool_input or tool.get("input") or tool.get("toolInput") or {}

    if not isinstance(tool_input, dict):
        normalized_input: dict[str, Any] = {"input": tool_input}
    else:
        normalized_input = tool_input.copy()

    if "command" in data and "command" not in normalized_input:
        normalized_input["command"] = data["command"]

    return normalized_input


def _extract_tool_call(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract tool name/input from Claude- and Codex-shaped hook payloads."""
    return _extract_tool_name(data), _extract_tool_input(data)


def _emit_deny(reason: str) -> None:
    """Write the CLI hook protocol's fail-closed decision shape."""
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        reason = "Malformed CLI hook input; failing closed"
        _log(reason)
        _emit_deny(reason)
        sys.exit(0)
    if not isinstance(data, dict):
        reason = "Malformed CLI hook input; failing closed"
        _log(reason)
        _emit_deny(reason)
        sys.exit(0)

    try:
        tool_name, tool_input = _extract_tool_call(data)
        hooks = _load_roster()
        decision = asyncio.run(_evaluate(hooks, tool_name, tool_input))
    except Exception as exc:  # allow: exception-handling  # noqa: BLE001
        reason = f"Built-in security gate failed closed: {type(exc).__name__}"
        _log(reason)
        _emit_deny(reason)
        sys.exit(0)

    if decision is not None:
        _emit_deny(decision.reason or "Blocked by security policy")
        _log(f"denied {tool_name}: {decision.reason or 'security policy'}")

    # Empty stdout + exit 0 == allow.
    sys.exit(0)


if __name__ == "__main__":
    main()
