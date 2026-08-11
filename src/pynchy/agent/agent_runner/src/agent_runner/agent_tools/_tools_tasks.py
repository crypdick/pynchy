"""Read and manage existing scheduled work."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool

from . import _ipc
from ._ipc_request import ipc_service_request
from ._registry import ToolEntry, register, tool, tool_error
from ._task_status_format import (
    TASK_STATUS_OUTPUT_SCHEMA,
    TaskStatusFormatError,
    compact_live_task_status,
)

_TASK_DEFINITION_FIELDS = (
    "id",
    "group",
    "prompt",
    "schedule_type",
    "schedule_value",
    "session_policy",
    "status",
    "memory_enabled",
)

_TASK_DEFINITION_SCHEMA = {
    "type": "object",
    "required": list(_TASK_DEFINITION_FIELDS),
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 128},
        "group": {"type": "string", "minLength": 1, "maxLength": 64},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
        "schedule_type": {"type": "string", "enum": ["cron", "interval", "once"]},
        "schedule_value": {"type": "string", "minLength": 1, "maxLength": 128},
        "session_policy": {"type": "string", "enum": ["continue", "reset_before_run"]},
        "status": {"type": "string", "enum": ["active", "paused", "completed", "cancelled"]},
        "memory_enabled": {"type": "boolean"},
    },
    "additionalProperties": False,
}


def _task_definition_tool(name: str, description: str, input_schema: dict[str, Any]) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        outputSchema=_TASK_DEFINITION_SCHEMA,
    )


async def _task_definition_request(
    request_kind: str, arguments: dict[str, Any]
) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    response = await ipc_service_request(
        request_kind,
        arguments,
        response_timeout_seconds=10,
        type_override=request_kind,
    )
    if not response or response[0].text.startswith("Error:"):
        return tool_error(response[0].text if response else "Error: empty host response")
    result, error = _parse_task_definition(response[0].text)
    if error is not None:
        return tool_error(error)
    return [TextContent(type="text", text=json.dumps(result, separators=(",", ":")))], result


def _parse_task_definition(raw: str) -> tuple[dict[str, Any], str | None]:
    result: dict[str, Any] = {}
    error: str | None = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        error = "Error: host scheduled task definition is not valid JSON"
    else:
        if isinstance(parsed, dict):
            result = parsed
        else:
            error = "Error: host scheduled task definition must be an object"
    if error is None:
        error = _task_definition_validation_error(result)
    return result, error


def _task_definition_validation_error(result: dict[str, Any]) -> str | None:
    if set(result) != set(_TASK_DEFINITION_FIELDS):
        return "Error: host scheduled task definition has invalid fields"
    text_fields = (
        "id",
        "group",
        "prompt",
        "schedule_type",
        "schedule_value",
        "session_policy",
        "status",
    )
    if not all(isinstance(result[field], str) and result[field] for field in text_fields):
        return "Error: host scheduled task definition has invalid text fields"
    if (
        result["schedule_type"] not in {"cron", "interval", "once"}
        or result["session_policy"] not in {"continue", "reset_before_run"}
        or result["status"] not in {"active", "paused", "completed", "cancelled"}
    ):
        return "Error: host scheduled task definition has invalid values"
    if not isinstance(result["memory_enabled"], bool):
        return "Error: host scheduled task definition has invalid memory setting"
    return None


def _get_scheduled_task_definition() -> Tool:
    return _task_definition_tool(
        "get_scheduled_task",
        "Read one visible scheduled task's editable definition, including its prompt.",
        _TASK_ID_SCHEMA,
    )


async def _get_scheduled_task_handle(
    arguments: dict[str, Any],
) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    return await _task_definition_request("task_definition", arguments)


_TASK_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
        "status": {"type": "string", "enum": ["active", "paused"]},
    },
    "required": ["task_id"],
    "anyOf": [{"required": ["prompt"]}, {"required": ["status"]}],
    "additionalProperties": False,
}


def _update_scheduled_task_definition() -> Tool:
    return _task_definition_tool(
        "update_scheduled_task",
        "Update the prompt or active/paused status of one visible scheduled task.",
        _TASK_UPDATE_SCHEMA,
    )


async def _update_scheduled_task_handle(
    arguments: dict[str, Any],
) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    return await _task_definition_request("update_scheduled_task", arguments)


def _list_tasks_definition() -> Tool:
    return Tool(
        name="list_tasks",
        description=(
            "Read a complete snapshot of current scheduled-work health for visible "
            "database-backed agent tasks and host jobs, including "
            "status, last results, recent failure summaries, Temporal next-run times, and "
            "orchestration errors. "
            "The result states its completeness scope and omitted scheduler populations; "
            "it does not include static config/plugin host schedules or Temporal schedules "
            "without a visible database-backed definition. "
            "Returns compact JSON in both text and MCP structured content. "
            "Call once, parse the JSON, and answer directly without loading skills or "
            "re-querying host state. "
            "From admin: shows all tasks across all groups. "
            "From other groups: shows only that group's agent tasks."
        ),
        inputSchema={"type": "object", "properties": {}},
        outputSchema=TASK_STATUS_OUTPUT_SCHEMA,
    )


async def _list_tasks_handle(
    _arguments: dict[str, Any],
) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    live_result = await ipc_service_request(
        "list_tasks",
        {},
        response_timeout_seconds=10,
        type_override="task_status",
    )
    if live_result and not live_result[0].text.startswith("Error:"):
        try:
            payload = compact_live_task_status(live_result[0].text)
        except TaskStatusFormatError as exc:
            live_result = [TextContent(type="text", text=f"Error: {exc}")]
        else:
            return (
                [
                    TextContent(
                        type="text",
                        text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    )
                ],
                payload,
            )

    live_error = live_result[0].text if live_result else "Error: empty host response"
    live_error = " ".join(live_error.split())
    if len(live_error) > 240:
        live_error = f"{live_error[:237]}..."
    return tool_error(f"{live_error}\nNo complete scheduled-work inventory is available.")


# -- pause/resume/cancel --

_TASK_ID_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The task ID",
        },
    },
    "required": ["task_id"],
}


def _task_action(action: str, task_id: str) -> list[TextContent]:
    """Write a pause/resume/cancel IPC file and return confirmation."""
    _ipc.write_request_file(
        action,
        {
            "taskId": task_id,
            "groupFolder": _ipc.get_agent_tool_runtime().group_folder,
            "isAdmin": _ipc.get_agent_tool_runtime().is_admin,
        },
        reply_to=None,
    )
    verb = action.replace("_task", "")
    if verb == "cancel":
        verb = "cancellation"
    return [TextContent(type="text", text=f"Task {task_id} {verb} requested.")]


@tool(
    "pause_task",
    "Pause a scheduled task or host job. It will not run until resumed.",
    _TASK_ID_SCHEMA,
)
async def _pause_task_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _task_action("pause_task", arguments["task_id"])


@tool("resume_task", "Resume a paused task or host job.", _TASK_ID_SCHEMA)
async def _resume_task_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _task_action("resume_task", arguments["task_id"])


@tool("cancel_task", "Cancel and delete a scheduled task or host job.", _TASK_ID_SCHEMA)
async def _cancel_task_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _task_action("cancel_task", arguments["task_id"])


register(
    "list_tasks",
    ToolEntry(definition=_list_tasks_definition, handler=_list_tasks_handle),
)
register(
    "get_scheduled_task",
    ToolEntry(definition=_get_scheduled_task_definition, handler=_get_scheduled_task_handle),
)
register(
    "update_scheduled_task",
    ToolEntry(
        definition=_update_scheduled_task_definition,
        handler=_update_scheduled_task_handle,
    ),
)
