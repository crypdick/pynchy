"""OpenAI Agents SDK tool call/result extraction.

The SDK emits different object shapes for different tool types (shell,
apply_patch, web_search, MCP, function).  These helpers normalize the
inconsistent representations into a uniform ``(tool_name, tool_input)``
tuple so the streaming loop in ``openai.py`` stays clean.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

SdkMapping = dict[str, object]
ToolPayload = object


def _nonempty_string(value: object) -> str | None:
    """Return a non-empty string or None for an untyped SDK value."""
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# SDK object normalization
# ---------------------------------------------------------------------------
# The OpenAI Agents SDK returns inconsistently-typed objects: sometimes dicts,
# sometimes Pydantic models, sometimes plain objects with __dict__.  These
# helpers normalize attribute access so the tool extraction code doesn't need
# to worry about the shape.


def _as_mapping(obj: object) -> SdkMapping | None:
    """Try to convert *obj* to a plain dict; return None on failure."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            data = obj.model_dump()
        except Exception:  # allow: exception-handling; best-effort  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            return data
    if hasattr(obj, "__dict__"):
        data = vars(obj)
        if isinstance(data, dict):
            return data
    return None


def _normalize_shell_action(action: object) -> SdkMapping | None:
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


def _initial_name_and_input(item: object, raw: object) -> tuple[str | None, ToolPayload]:
    """First-pass tool_name/tool_input guess from direct SDK attributes."""
    tool_name: str | None = (
        getattr(item, "tool_name", None)
        or getattr(item, "name", None)
        or getattr(raw, "tool_name", None)
        or getattr(raw, "name", None)
    )
    tool_input: ToolPayload = (
        getattr(item, "arguments", None)
        or getattr(item, "input", None)
        or getattr(raw, "arguments", None)
    )
    return tool_name, tool_input


def _fill_from_function_or_call_subobject(
    raw: object, tool_name: str | None, tool_input: ToolPayload
) -> tuple[str | None, ToolPayload]:
    for attr in ("function", "call"):
        sub = getattr(raw, attr, None)
        if sub is not None:
            tool_name = tool_name or getattr(sub, "name", None)
            tool_input = tool_input or getattr(sub, "arguments", None)
    return tool_name, tool_input


def _fill_from_shell_action(
    raw: object, tool_name: str | None, tool_input: ToolPayload
) -> tuple[str | None, ToolPayload]:
    """Shell action may be nested under raw.data instead of directly on raw."""
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
    return tool_name, tool_input


def _normalize_tool_input(raw: object, tool_input: ToolPayload) -> ToolPayload:
    if tool_input is None:
        tool_input = getattr(raw, "input", None)
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {"raw": tool_input}
    return tool_input


def extract_tool_call(item: object) -> tuple[str, ToolPayload]:
    """Extract (tool_name, tool_input) from an OpenAI SDK tool_call_item.

    Tries every known attribute path in priority order and falls back to
    heuristics on the raw type name or input contents.
    """
    raw = getattr(item, "raw_item", item)
    tool_name, tool_input = _initial_name_and_input(item, raw)
    tool_name, tool_input = _fill_from_function_or_call_subobject(raw, tool_name, tool_input)
    tool_name, tool_input = _fill_from_shell_action(raw, tool_name, tool_input)

    # Type-specific extraction based on raw_type
    raw_map = _as_mapping(raw)
    raw_type = _nonempty_string(raw_map.get("type") if raw_map else None) or _nonempty_string(
        getattr(raw, "type", None)
    )

    tool_name, tool_input = _extract_by_raw_type(raw_type, raw, raw_map, tool_name, tool_input)

    # Last-resort scan of the full object as a dict
    tool_name, tool_input = _fallback_mapping_scan(raw, raw_map, tool_name, tool_input)

    # Heuristics on the Python type name
    if tool_name in _UNKNOWN_NAMES:
        tool_name = _guess_from_type_name(raw, raw_type)

    # Heuristics on tool_input keys
    if tool_name in _UNKNOWN_NAMES:
        tool_name = _guess_from_input(tool_input)

    tool_input = _normalize_tool_input(raw, tool_input)

    return tool_name or "unknown_tool", tool_input


def extract_tool_result(item: object) -> tuple[str, str, bool]:
    """Extract (tool_result_id, output, is_error) from an SDK tool output item."""
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
    return (
        str(tool_result_id) if tool_result_id else "",
        str(output) if output else "",
        _tool_result_is_error(raw_map),
    )


def _tool_result_is_error(raw_item: SdkMapping) -> bool:
    # Use only structured states; tool text can contain credentials or other secrets.
    if raw_item.get("status") in {"failed", "incomplete"}:
        return True
    if raw_item.get("type") != "shell_call_output":
        return False
    outputs = raw_item.get("output")
    return isinstance(outputs, list) and any(_shell_output_is_error(output) for output in outputs)


def _shell_output_is_error(output: object) -> bool:
    output_map = _as_mapping(output)
    if output_map is None:
        return False
    outcome = _as_mapping(output_map.get("outcome"))
    if outcome is None:
        return False
    if outcome.get("type") == "timeout":
        return True
    exit_code = outcome.get("exit_code")
    return isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_shell_call(
    raw: object,
    raw_map: SdkMapping | None,
    tool_name: str | None,
    tool_input: ToolPayload,
) -> tuple[str | None, ToolPayload]:
    tool_name = tool_name or "shell"
    if tool_input is None:
        am = (
            _normalize_shell_action(raw_map.get("action")) if raw_map else None
        ) or _normalize_shell_action(getattr(raw, "action", None))
        if am:
            tool_input = am
    return tool_name, tool_input


def _extract_apply_patch_call(
    raw: object,
    raw_map: SdkMapping | None,
    tool_name: str | None,
    tool_input: ToolPayload,
) -> tuple[str | None, ToolPayload]:
    tool_name = tool_name or "apply_patch"
    if tool_input is None:
        operation = (raw_map.get("operation") if raw_map else None) or getattr(
            raw, "operation", None
        )
        tool_input = _as_mapping(operation) or operation
    return tool_name, tool_input


def _extract_web_search_call(
    raw: object,
    raw_map: SdkMapping | None,
    tool_name: str | None,
    tool_input: ToolPayload,
) -> tuple[str | None, ToolPayload]:
    tool_name = tool_name or "web_search"
    if tool_input is None:
        action = raw_map.get("action") if raw_map else getattr(raw, "action", None)
        tool_input = _as_mapping(action) or action
    return tool_name, tool_input


def _extract_function_or_mcp_call(
    raw: object,
    raw_map: SdkMapping | None,
    tool_name: str | None,
    tool_input: ToolPayload,
) -> tuple[str | None, ToolPayload]:
    if tool_name in _UNKNOWN_NAMES:
        tool_name = _nonempty_string(raw_map.get("name") if raw_map else None) or _nonempty_string(
            getattr(raw, "name", None)
        )
    if tool_input is None and raw_map:
        tool_input = raw_map.get("arguments") or raw_map.get("input")
    return tool_name, tool_input


_RAW_TYPE_EXTRACTORS: dict[
    str,
    Callable[
        [object, SdkMapping | None, str | None, ToolPayload],
        tuple[str | None, ToolPayload],
    ],
] = {
    "shell_call": _extract_shell_call,
    "local_shell_call": _extract_shell_call,
    "apply_patch_call": _extract_apply_patch_call,
    "web_search_call": _extract_web_search_call,
    "function_call": _extract_function_or_mcp_call,
    "mcp_call": _extract_function_or_mcp_call,
}


def _extract_by_raw_type(
    raw_type: str | None,
    raw: object,
    raw_map: SdkMapping | None,
    tool_name: str | None,
    tool_input: ToolPayload,
) -> tuple[str | None, ToolPayload]:
    """Refine tool_name/tool_input based on the ``raw_type`` field."""
    extractor = _RAW_TYPE_EXTRACTORS.get(raw_type) if raw_type else None
    if extractor is None:
        return tool_name, tool_input
    return extractor(raw, raw_map, tool_name, tool_input)


def _fallback_data_dump(raw: object, raw_map: SdkMapping | None) -> SdkMapping | None:
    """Best-effort dict view of the raw SDK object for mapping-based scanning."""
    if isinstance(raw_map, dict):
        return raw_map
    if hasattr(raw, "__dict__"):
        data_dump = vars(raw)
        if isinstance(data_dump, dict):
            return data_dump
    return None


def _fallback_mappings(data_dump: SdkMapping) -> tuple[SdkMapping, ...]:
    """Mappings to inspect during the final mapping scan."""
    nested_data = data_dump.get("data")
    return (data_dump, nested_data) if isinstance(nested_data, dict) else (data_dump,)


def _fallback_tool_name(mapping: SdkMapping, tool_name: str | None) -> str | None:
    """Fill in the tool name from common mapping keys."""
    if tool_name not in _UNKNOWN_NAMES:
        return tool_name
    for key in ("tool_name", "name", "tool", "type"):
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return tool_name


def _fallback_tool_input(mapping: SdkMapping, tool_input: ToolPayload) -> ToolPayload:
    """Fill in the tool input from common mapping keys."""
    if tool_input is not None:
        return tool_input
    return mapping.get("arguments") or mapping.get("input")


def _fallback_action_data(
    mapping: SdkMapping, tool_name: str | None, tool_input: ToolPayload
) -> tuple[str | None, ToolPayload]:
    """Extract shell-shaped info from nested action payloads."""
    action = mapping.get("action")
    if not isinstance(action, dict):
        return tool_name, tool_input

    if tool_input is None:
        commands = action.get("commands")
        command = action.get("command")
        if commands or command:
            tool_input = {"commands": commands} if commands else {"command": command}

    if tool_name in _UNKNOWN_NAMES and action.get("type") in ("exec", "shell", "shell_call"):
        tool_name = "shell"

    return tool_name, tool_input


def _fallback_mapping_scan(
    raw: object,
    raw_map: SdkMapping | None,
    tool_name: str | None,
    tool_input: ToolPayload,
) -> tuple[str | None, ToolPayload]:
    """Scan the raw object's dict representation for tool name/input as a last resort."""
    data_dump = _fallback_data_dump(raw, raw_map)
    if data_dump is None:
        return tool_name, tool_input

    for mapping in _fallback_mappings(data_dump):
        tool_name = _fallback_tool_name(mapping, tool_name)
        tool_input = _fallback_tool_input(mapping, tool_input)
        tool_name, tool_input = _fallback_action_data(mapping, tool_name, tool_input)

    return tool_name, tool_input


def _guess_from_type_name(raw: object, raw_type: str | None) -> str:
    """Guess tool name from the Python type name of the raw SDK object."""
    raw_type_name = type(raw).__name__.lower()
    if "shell" in raw_type_name:
        return "shell"
    if "patch" in raw_type_name:
        return "apply_patch"
    if "search" in raw_type_name:
        return "web_search"
    return raw_type or _nonempty_string(getattr(raw, "type", None)) or "unknown_tool"


def _guess_from_input(tool_input: ToolPayload) -> str:
    """Guess tool name from the shape of tool_input."""
    if not tool_input:
        return "shell"
    if isinstance(tool_input, dict):
        if "patch" in tool_input or "path" in tool_input:
            return "apply_patch"
        if "query" in tool_input or "q" in tool_input:
            return "web_search"
    return "unknown_tool"
