"""Parse the public ``codex exec --json`` thread-item shapes."""

from __future__ import annotations

import json


def item_text(item: dict[str, object]) -> str:
    """Extract display text from common Codex JSONL item shapes."""
    text = (
        item.get("text")
        or item.get("message")
        or item.get("summary")
        or item.get("aggregated_output")
        or item.get("output")
    )
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "\n".join(_content_text(part) for part in text)
    result = item.get("result")
    if isinstance(result, dict):
        structured = result.get("structured_content")
        if structured is not None:
            return _content_text(structured)
        content = result.get("content")
        if isinstance(content, list):
            return "\n".join(_content_text(part) for part in content)
    error = item.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])
    return ""


def _content_text(value: object) -> str:
    """Render one JSON-shaped Codex result block without exposing object reprs."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _command_string(value: object, joiner: str) -> str:
    if isinstance(value, list):
        return joiner.join(str(part) for part in value)
    if value is not None:
        return str(value)
    return ""


def _action_command(action: dict[str, object]) -> str:
    commands = _command_string(action.get("commands"), " && ")
    if commands:
        return commands
    return _command_string(action.get("command"), " ")


def item_command(item: dict[str, object]) -> str:
    command = _command_string(item.get("command") or item.get("cmd"), " ")
    if command:
        return command
    action = item.get("action")
    return _action_command(action) if isinstance(action, dict) else ""


def item_id(item: dict[str, object]) -> str:
    return str(item.get("id") or item.get("call_id") or item.get("callId") or "")


def item_is_error(item: dict[str, object]) -> bool:
    status = item.get("status")
    return bool(
        item.get("is_error")
        or item.get("isError")
        or item.get("error")
        or status in {"failed", "declined"}
    )


def file_change_input(item: dict[str, object]) -> dict[str, object]:
    changes = item.get("changes")
    return {"changes": changes if isinstance(changes, list) else []}


def file_change_result(item: dict[str, object]) -> str:
    return json.dumps(
        {
            "changes": file_change_input(item)["changes"],
            "status": item.get("status", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
