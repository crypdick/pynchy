"""CLI ``PreToolUse`` hook entrypoint for the claude-cli agent core.

The Claude Code CLI runs ``PreToolUse`` hooks as shell commands: it passes the
tool call as JSON on the command's **stdin** and reads a decision JSON from its
**stdout**. This module bridges that protocol back to pynchy's in-Python
BEFORE_TOOL_USE security functions (``bash_security_hook``, ``guard_git_hook``),
so the claude-cli core enforces the exact same gate as the SDK core
(cores/claude.py, ``_wrap_before_tool_use``).

The gate itself is stateless in the container -- ``bash_security_hook``
delegates taint/Cop decisions to the host over file IPC -- so evaluating it from
this short-lived subprocess is functionally identical to the SDK's in-process
call. Invoked as ``python -m agent_runner.security.hook_entry``.
"""

from __future__ import annotations

import asyncio
import json
import sys

from agent_runner.hooks import HookDecision, builtin_before_tool_hooks

# Built-in gate, single-sourced from the shared roster so every core (SDK,
# OpenAI, and this CLI entrypoint) enforces the same hooks in the same order:
# first deny wins.
#
# NOTE: this is the *built-in* roster only. Unlike the SDK core -- which also
# registers plugin-provided BEFORE_TOOL_USE hooks (load_hooks(plugin_hooks)) --
# this entrypoint runs no plugin hooks. Moot today because main.py wires
# plugin_hooks=[] for every core, but if that TODO is resolved this must grow to
# load and run the plugin BEFORE_TOOL_USE roster too, or the claude-cli core
# will silently enforce fewer hooks than the SDK core.
_HOOKS = tuple(builtin_before_tool_hooks())


def _log(message: str) -> None:
    print(f"[claude-cli-hook] {message}", file=sys.stderr, flush=True)


async def _evaluate(tool_name: str, tool_input: dict) -> HookDecision | None:
    """Run the built-in gate; return the first denying decision, else None."""
    for hook in _HOOKS:
        decision = await hook(tool_name, tool_input)
        if not decision.allowed:
            return decision
    return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Malformed hook payload is a harness bug, not an attack surface. Failing
        # closed here would brick every tool call; fail open and log loudly. The
        # bash gate itself remains the real control on well-formed calls.
        _log(f"unparseable hook input, allowing by default: {raw[:200]}")
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    try:
        decision = asyncio.run(_evaluate(tool_name, tool_input))
    except Exception as exc:  # noqa: BLE001 - never crash the agent's tool loop
        _log(f"gate evaluation error, allowing by default: {exc}")
        sys.exit(0)

    if decision is not None:
        # Same deny shape the SDK core emits via _wrap_before_tool_use.
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (decision.reason or "Blocked by security policy"),
                }
            },
            sys.stdout,
        )
        _log(f"denied {tool_name}: {decision.reason or 'security policy'}")

    # Empty stdout + exit 0 == allow.
    sys.exit(0)


if __name__ == "__main__":
    main()
