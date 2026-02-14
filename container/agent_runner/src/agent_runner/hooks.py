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
from collections.abc import Callable
from enum import StrEnum


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
    """Fired when a new session is initialized."""

    SESSION_END = "session_end"
    """Fired when a session ends."""

    ERROR = "error"
    """Fired when an error occurs during query execution."""


# Mapping from Claude SDK hook names to agnostic events
# Used by ClaudeAgentCore to reverse-map from agnostic events
CLAUDE_HOOK_MAP: dict[str, HookEvent] = {
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


def load_hooks(plugin_hooks: list[dict[str, str]]) -> dict[HookEvent, list[Callable]]:
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
    hooks: dict[HookEvent, list[Callable]] = {event: [] for event in HookEvent}

    for spec in plugin_hooks:
        name = spec.get("name", "unknown")
        module_path = spec.get("module_path")

        if not module_path:
            print(f"[agent-runner] Hook '{name}' missing module_path, skipping", file=sys.stderr)
            continue

        try:
            # Load module from file path
            spec_obj = importlib.util.spec_from_file_location(f"hook_{name}", module_path)
            if spec_obj is None or spec_obj.loader is None:
                print(
                    f"[agent-runner] Failed to load hook '{name}' from {module_path}",
                    file=sys.stderr,
                )
                continue

            module = importlib.util.module_from_spec(spec_obj)
            sys.modules[f"hook_{name}"] = module
            spec_obj.loader.exec_module(module)

            # Look for hook functions matching event names
            for event in HookEvent:
                func_name = event.value  # e.g., "before_compact"
                if hasattr(module, func_name):
                    func = getattr(module, func_name)
                    if callable(func):
                        hooks[event].append(func)

        except Exception as exc:
            print(f"[agent-runner] Failed to load hook '{name}': {exc}", file=sys.stderr)

    return hooks
