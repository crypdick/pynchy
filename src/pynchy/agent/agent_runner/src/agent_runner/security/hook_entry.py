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

from agent_runner.hooks import HookDecision
from agent_runner.security.bash_gate import bash_security_hook
from agent_runner.security.guard_git import guard_git_hook

# Built-in gate, in the same order the SDK core registers it: first deny wins.
_HOOKS = (bash_security_hook, guard_git_hook)


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
