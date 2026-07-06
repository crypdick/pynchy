"""Hook event abstraction for framework-agnostic lifecycle events.

Different agent frameworks have different hook systems. This module defines
core-agnostic lifecycle events that can be mapped to framework-specific hooks.

Each core translates these events to its native hook system. For example:
- ClaudeAgentCore maps to Claude SDK hooks (PreCompact, PostCompact, etc.)
- OpenAI cores would map to their equivalent lifecycle points
- Cores can silently ignore unsupported events
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class HookEvent(StrEnum):
    """Core-agnostic hook event types."""

    BEFORE_COMPACT = "before_compact"
    """Fired before conversation history is compacted/summarized."""

    AFTER_COMPACT = "after_compact"
    """Fired after conversation history is compacted/summarized."""

    BEFORE_QUERY = "before_query"
    """Fired before each LLM query."""

    AFTER_QUERY = "after_query"
    """Fired after each LLM query completes."""

    SESSION_START = "session_start"
    """Fired when a session is initialized."""

    SESSION_END = "session_end"
    """Fired when a session ends."""

    BEFORE_TOOL_USE = "before_tool_use"
    """Fired before a tool is executed. Can return deny to block."""

    ERROR = "error"
    """Fired when an error occurs during query execution."""


# Mapping from Claude SDK hook names to agnostic events
# Used by ClaudeAgentCore to reverse-map from agnostic events
CLAUDE_HOOK_MAP: dict[str, HookEvent] = {
    "PreToolUse": HookEvent.BEFORE_TOOL_USE,
    "PreCompact": HookEvent.BEFORE_COMPACT,
    "PostCompact": HookEvent.AFTER_COMPACT,
    "PreQuery": HookEvent.BEFORE_QUERY,
    "PostQuery": HookEvent.AFTER_QUERY,
    "SessionStart": HookEvent.SESSION_START,
    "SessionEnd": HookEvent.SESSION_END,
    "Error": HookEvent.ERROR,
}

# Reverse mapping: agnostic event → Claude SDK hook name
AGNOSTIC_TO_CLAUDE: dict[HookEvent, str] = {v: k for k, v in CLAUDE_HOOK_MAP.items()}


@dataclass
class HookDecision:
    """Result of a before_tool_use hook evaluation."""

    allowed: bool = True
    reason: str | None = None


# A plugin-provided lifecycle hook. Signature varies by event (compact hooks
# take (input_data, tool_use_id, context); before-tool hooks take the pair
# below), so the general load_hooks map holds the permissive form.
HookFn = Callable[..., Any]

# A BEFORE_TOOL_USE gate: (tool_name, tool_input) -> HookDecision.
BeforeToolUseHook = Callable[[str, dict[str, Any]], Awaitable[HookDecision]]


def load_hooks(plugin_hooks: list[dict[str, str]]) -> dict[HookEvent, list[HookFn]]:
    """Load hook functions from plugin module paths.

    Args:
        plugin_hooks: List of hook specifications with 'name' and 'module_path' keys
                     Example: [{"name": "my-hook",
                               "module_path": "/workspace/plugins/my-hook/hook.py"}]

    Returns:
        Dict mapping HookEvent to list of callable hook functions

    Hook modules should export hook functions by event name:
        before_compact(input_data, tool_use_id, context) -> dict
        after_compact(input_data, tool_use_id, context) -> dict
        etc.
    """
    hooks: dict[HookEvent, list[HookFn]] = {event: [] for event in HookEvent}

    for spec in plugin_hooks:
        name = spec.get("name", "unknown")
        module_path = spec.get("module_path")

        if not module_path:
            print(  # allow: print-statements — stderr diagnostic channel; no logger available
                f"[agent-runner] Hook '{name}' missing module_path, skipping",
                file=sys.stderr,
            )
            continue

        try:
            # Load module from file path
            spec_obj = importlib.util.spec_from_file_location(f"hook_{name}", module_path)
            if spec_obj is None or spec_obj.loader is None:
                print(  # allow: print-statements — stderr diagnostic channel; no logger available
                    f"[agent-runner] Failed to load hook '{name}' from {module_path}",
                    file=sys.stderr,
                )
                continue

            module = importlib.util.module_from_spec(spec_obj)
            sys.modules[f"hook_{name}"] = module
            spec_obj.loader.exec_module(module)

            # Look for hook functions matching event names
            registered_events: list[str] = []
            for event in HookEvent:
                func_name = event.value  # e.g., "before_compact"
                if hasattr(module, func_name):
                    func = getattr(module, func_name)
                    if callable(func):
                        hooks[event].append(func)
                        registered_events.append(func_name)

            if registered_events:
                print(  # allow: print-statements — stderr diagnostic channel; no logger available
                    f"[agent-runner] Loaded hook '{name}': {', '.join(registered_events)}",
                    file=sys.stderr,
                )
            else:
                print(  # allow: print-statements — stderr diagnostic channel; no logger available
                    f"[agent-runner] Hook '{name}' loaded but has no event handlers",
                    file=sys.stderr,
                )

        except Exception as exc:  # allow: exception-handling — one bad hook must not block others
            print(  # allow: print-statements — stderr diagnostic channel; no logger available
                f"[agent-runner] Failed to load hook '{name}': {exc}",
                file=sys.stderr,
            )

    return hooks


def builtin_before_tool_hooks() -> list[BeforeToolUseHook]:
    """Return the built-in BEFORE_TOOL_USE security hooks, in enforcement order.

    Built-ins run before any plugin-provided BEFORE_TOOL_USE hooks. Callers
    should not compose the full gate by hand -- use
    :func:`before_tool_use_roster` so every core enforces the same set.
    """
    from agent_runner.security.bash_gate import bash_security_hook
    from agent_runner.security.guard_git import guard_git_hook

    return [bash_security_hook, guard_git_hook]


def before_tool_use_roster(
    agnostic_hooks: dict[HookEvent, list[HookFn]],
) -> list[BeforeToolUseHook]:
    """The complete BEFORE_TOOL_USE gate every core must enforce, in order.

    Built-in security hooks first, then plugin-provided BEFORE_TOOL_USE hooks;
    first deny wins. This is the *single source of truth* for the security
    roster: every core composes its gate here -- the SDK core (cores/claude.py),
    the OpenAI core (cores/openai.py), and the claude-cli ``PreToolUse``
    subprocess (security/hook_entry.py). Routing all three through one function
    makes it impossible for a core to silently enforce a different set than the
    others (the exact drift that let the CLI subprocess run builtins-only).

    ``agnostic_hooks`` is the already-loaded :func:`load_hooks` map; callers that
    only have raw specs pass ``load_hooks(specs)``.
    """
    return [*builtin_before_tool_hooks(), *agnostic_hooks.get(HookEvent.BEFORE_TOOL_USE, [])]
