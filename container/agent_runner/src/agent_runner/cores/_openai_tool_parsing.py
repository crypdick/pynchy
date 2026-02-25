"""OpenAI Agents SDK tool call/result extraction.

The SDK emits different object shapes for different tool types (shell,
apply_patch, web_search, MCP, function).  These helpers normalize the
inconsistent representations into a uniform ``(tool_name, tool_input)``
tuple so the streaming loop in ``openai.py`` stays clean.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# SDK object normalization
# ---------------------------------------------------------------------------
# The OpenAI Agents SDK returns inconsistently-typed objects: sometimes dicts,
# sometimes Pydantic models, sometimes plain objects with __dict__.  These
# helpers normalize attribute access so the tool extraction code doesn't need
# to worry about the shape.


def _as_mapping(obj: Any) -> dict[str, Any] | None:
    """Try to convert *obj* to a plain dict; return None on failure."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            data = obj.model_dump()
        except Exception:
            data = None
        if isinstance(data, dict):
            return data
    if hasattr(obj, "__dict__"):
        data = vars(obj)
        if isinstance(data, dict):
            return data
    return None


def _normalize_shell_action(action: Any) -> dict[str, Any] | None:
    """Normalize a shell action to a dict with ``commands`` key."""
    action_map = _as_mapping(action)
    if not action_map:
        return None
    # Local shell calls use "command" (list[str]); normalize to "commands".
    if "commands" not in action_map and "command" in action_map:
        action_map = dict(action_map)
        action_map["commands"] = action_map.get("command")
    return action_map


# Sentinel names that mean "we couldn't identify the tool"
_UNKNOWN_NAMES = (None, "", "unknown_tool")


# ---------------------------------------------------------------------------
# Public extraction functions
# ---------------------------------------------------------------------------


def extract_tool_call(item: Any) -> tuple[str, Any]:
    """Extract (tool_name, tool_input) from an OpenAI SDK tool_call_item.

    Tries every known attribute path in priority order and falls back to
    heuristics on the raw type name or input contents.
    """
    raw = getattr(item, "raw_item", item)
    tool_name: str | None = (
        getattr(item, "tool_name", None)
        or getattr(item, "name", None)
        or getattr(raw, "tool_name", None)
        or getattr(raw, "name", None)
    )
    tool_input: Any = (
        getattr(item, "arguments", None)
        or getattr(item, "input", None)
        or getattr(raw, "arguments", None)
    )

    # function / call sub-objects
    for attr in ("function", "call"):
        sub = getattr(raw, attr, None)
        if sub is not None:
            tool_name = tool_name or getattr(sub, "name", None)
            tool_input = tool_input or getattr(sub, "arguments", None)

    # action sub-object (may be nested under raw.data)
    action = getattr(raw, "action", None)
    if action is None:
        data_obj = getattr(raw, "data", None)
        action = getattr(data_obj, "action", None) if data_obj is not None else None
    action_map = _normalize_shell_action(action)
    if action_map:
        if tool_name in (*_UNKNOWN_NAMES, "function"):
            tool_name = "shell"
        if tool_input is None:
            tool_input = action_map

    # Type-specific extraction based on raw_type
    raw_map = _as_mapping(raw)
    raw_type: str | None = (raw_map.get("type") if raw_map else None) or getattr(raw, "type", None)

    tool_name, tool_input = _extract_by_raw_type(raw_type, raw, raw_map, tool_name, tool_input)

    # Last-resort scan of the full object as a dict
    tool_name, tool_input = _fallback_mapping_scan(raw, raw_map, tool_name, tool_input)

    # Heuristics on the Python type name
    if tool_name in _UNKNOWN_NAMES:
        tool_name = _guess_from_type_name(raw, raw_type)

    # Heuristics on tool_input keys
    if tool_name in _UNKNOWN_NAMES:
        tool_name = _guess_from_input(tool_input)

    # Normalize tool_input to a dict
    if tool_input is None:
        tool_input = getattr(raw, "input", None)
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {"raw": tool_input}

    return tool_name or "unknown_tool", tool_input


def extract_tool_result(item: Any) -> tuple[str, str]:
    """Extract (tool_result_id, output) from an OpenAI SDK tool_call_output_item."""
    output = getattr(item, "output", "")
    raw = getattr(item, "raw_item", item)
    raw_map = _as_mapping(raw) or {}
    tool_result_id = (
        getattr(item, "call_id", None)
        or getattr(raw, "call_id", None)
        or raw_map.get("call_id")
        or raw_map.get("id")
        or ""
    )
    return tool_result_id, str(output) if output else ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_by_raw_type(
    raw_type: str | None,
    raw: Any,
    raw_map: dict[str, Any] | None,
    tool_name: str | None,
    tool_input: Any,
) -> tuple[str | None, Any]:
    """Refine tool_name/tool_input based on the ``raw_type`` field."""
    if raw_type in ("shell_call", "local_shell_call"):
        tool_name = tool_name or "shell"
        if tool_input is None:
            am = (
                _normalize_shell_action(raw_map.get("action")) if raw_map else None
            ) or _normalize_shell_action(getattr(raw, "action", None))
            if am:
                tool_input = am

    elif raw_type == "apply_patch_call":
        tool_name = tool_name or "apply_patch"
        if tool_input is None:
            operation = (raw_map.get("operation") if raw_map else None) or getattr(
                raw, "operation", None
            )
            tool_input = _as_mapping(operation) or operation

    elif raw_type == "web_search_call":
        tool_name = tool_name or "web_search"
        if tool_input is None:
            action = raw_map.get("action") if raw_map else getattr(raw, "action", None)
            tool_input = _as_mapping(action) or action

    elif raw_type in ("function_call", "mcp_call"):
        if tool_name in _UNKNOWN_NAMES:
            tool_name = (
                raw_map.get("name") if raw_map and raw_map.get("name") else None
            ) or getattr(raw, "name", None)
        if tool_input is None and raw_map:
            tool_input = raw_map.get("arguments") or raw_map.get("input")

    return tool_name, tool_input


def _fallback_mapping_scan(
    raw: Any,
    raw_map: dict[str, Any] | None,
    tool_name: str | None,
    tool_input: Any,
) -> tuple[str | None, Any]:
    """Scan the raw object's dict representation for tool name/input as a last resort."""
    data_dump = raw_map
    if data_dump is None and hasattr(raw, "__dict__"):
        data_dump = vars(raw)
    if not isinstance(data_dump, dict):
        return tool_name, tool_input

    for mapping in (data_dump, data_dump.get("data")):
        if not isinstance(mapping, dict):
            continue
        if tool_name in _UNKNOWN_NAMES:
            for key in ("tool_name", "name", "tool", "type"):
                value = mapping.get(key)
                if value:
                    tool_name = value
                    break
        if tool_input is None:
            tool_input = mapping.get("arguments") or mapping.get("input")
        action = mapping.get("action")
        if isinstance(action, dict):
            if tool_input is None:
                cmds = action.get("commands")
                cmd = action.get("command")
                if cmds or cmd:
                    tool_input = {"commands": cmds} if cmds else {"command": cmd}
            if tool_name in _UNKNOWN_NAMES and action.get("type") in (
                "exec",
                "shell",
                "shell_call",
            ):
                tool_name = "shell"

    return tool_name, tool_input


def _guess_from_type_name(raw: Any, raw_type: str | None) -> str:
    """Guess tool name from the Python type name of the raw SDK object."""
    raw_type_name = type(raw).__name__.lower()
    if "shell" in raw_type_name:
        return "shell"
    if "patch" in raw_type_name:
        return "apply_patch"
    if "search" in raw_type_name:
        return "web_search"
    return raw_type or getattr(raw, "type", None) or "unknown_tool"


def _guess_from_input(tool_input: Any) -> str:
    """Guess tool name from the shape of tool_input."""
    if not tool_input:
        return "shell"
    if isinstance(tool_input, dict):
        if "patch" in tool_input or "path" in tool_input:
            return "apply_patch"
        if "query" in tool_input or "q" in tool_input:
            return "web_search"
    return "unknown_tool"
