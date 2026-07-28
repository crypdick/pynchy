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


def _list_tasks_definition() -> Tool:
    return Tool(
        name="list_tasks",
        description=(
            "Read a bounded snapshot of current scheduled-work health for visible "
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


async def _list_tasks_handle(  # noqa: RUF029, RUF100 - async tool API.
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
    return tool_error(f"{live_error}\nNo complete bounded scheduled-work inventory is available.")


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
async def _pause_task_handle(  # noqa: RUF029, RUF100 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _task_action("pause_task", arguments["task_id"])


@tool("resume_task", "Resume a paused task or host job.", _TASK_ID_SCHEMA)
async def _resume_task_handle(  # noqa: RUF029, RUF100 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _task_action("resume_task", arguments["task_id"])


@tool("cancel_task", "Cancel and delete a scheduled task or host job.", _TASK_ID_SCHEMA)
async def _cancel_task_handle(  # noqa: RUF029, RUF100 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _task_action("cancel_task", arguments["task_id"])


register(
    "list_tasks",
    ToolEntry(definition=_list_tasks_definition, handler=_list_tasks_handle),
)
