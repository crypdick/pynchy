"""Task scheduling and management tools: schedule_task, list_tasks, pause/resume/cancel."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from croniter import croniter
from mcp.types import CallToolResult, TextContent, Tool

from . import _ipc
from ._ipc_request import ipc_service_request
from ._registry import ToolEntry, register, tool, tool_error
from ._task_status_format import (
    TASK_STATUS_OUTPUT_SCHEMA,
    TaskStatusFormatError,
    compact_live_task_status,
)

# -- schedule_task --


def _schedule_task_definition() -> Tool:
    return Tool(
        name="schedule_task",
        description=(
            "Schedule a recurring or one-time task.\n\n"
            "TASK TYPES:\n"
            '\u2022 "agent" (default): Runs a full agent with access '
            "to all tools in a container. Use for tasks requiring "
            "reasoning, tool use, or user interaction.\n"
            '\u2022 "host" (admin group only): Runs a shell command '
            "directly on the host. Use for system maintenance tasks. "
            "NOTE: Future improvement will add deputy agent review "
            "for security validation.\n\n"
            "SESSION POLICY (agent tasks only):\n"
            '\u2022 "group": Continue the task thread\'s durable session.\n'
            '\u2022 "isolated": Reset the task thread immediately before '
            "each scheduled occurrence. The thread and its new session remain "
            "durable after the run.\n\n"
            "If unsure which mode to use, you can ask the user. "
            "Examples:\n"
            '- "Remind me about our discussion" \u2192 group '
            "(needs conversation context)\n"
            '- "Check the weather every morning" \u2192 isolated '
            "(self-contained task)\n"
            '- "Follow up on my request" \u2192 group '
            "(needs to know what was requested)\n"
            '- "Generate a daily report" \u2192 isolated '
            "(just needs instructions in prompt)\n\n"
            "MESSAGING BEHAVIOR (agent tasks) - The task agent's "
            "output is sent to the user or group. It can also use "
            "send_message for immediate delivery, or wrap output in "
            "<internal> tags to suppress it. Include guidance in the "
            "prompt about whether the agent should:\n"
            "\u2022 Always send a message (e.g., reminders, daily "
            "briefings)\n"
            "\u2022 Only send a message when there's something to "
            'report (e.g., "notify me if...")\n'
            "\u2022 Never send a message (background maintenance "
            "tasks)\n\n"
            "SCHEDULE VALUE FORMAT (all times are LOCAL timezone):\n"
            '\u2022 cron: Standard cron expression (e.g., "*/5 * * '
            '* *" for every 5 minutes, "0 9 * * *" for daily at '
            "9am LOCAL time)\n"
            "\u2022 interval: Milliseconds between runs (e.g., "
            '"300000" for 5 minutes, "3600000" for 1 hour)\n'
            '\u2022 once: Local time WITHOUT "Z" suffix (e.g., '
            '"2026-02-01T15:30:00"). Do NOT use UTC/Z suffix.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "enum": ["agent", "host"],
                    "default": "agent",
                    "description": (
                        "agent=containerized LLM task, host=shell command (admin only)"
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "For agent tasks: What the agent should do. "
                        "For host tasks: ignored (use command field)."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": (
                        "For host tasks: Shell command to execute. For agent tasks: ignored."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "For host tasks: Unique job name (required). For agent tasks: ignored."
                    ),
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["cron", "interval", "once"],
                    "description": (
                        "cron=recurring at specific times, "
                        "interval=recurring every N ms, "
                        "once=run once at specific time"
                    ),
                },
                "schedule_value": {
                    "type": "string",
                    "description": (
                        'cron: "*/5 * * * *" | interval: '
                        'milliseconds like "300000" | once: '
                        "local timestamp like "
                        '"2026-02-01T15:30:00" (no Z suffix!)'
                    ),
                },
                "target_group": {
                    "type": "string",
                    "description": (
                        "(Admin group only) Folder name of the group to "
                        "schedule the task for (e.g. 'code-improver'). "
                        "Defaults to the current group."
                    ),
                },
                "context_mode": {
                    "type": "string",
                    "enum": ["group", "isolated"],
                    "default": "isolated",
                    "description": (
                        "Compatibility name for session policy: group=continue, "
                        "isolated=reset before each occurrence."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Host tasks only: Working directory for "
                        "command execution. Defaults to project root."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": 600,
                    "description": "Host tasks only: Command timeout in seconds.",
                },
            },
            "required": ["schedule_type", "schedule_value"],
        },
    )


async def _schedule_task_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    task_type = _task_type(arguments)

    task_type_error = _validate_task_type(task_type)
    if task_type_error:
        return task_type_error

    permission_error = _host_task_permission_error(task_type)
    if permission_error:
        return permission_error

    required_field_error = _required_task_field_error(arguments, task_type)
    if required_field_error:
        return required_field_error

    schedule_type = arguments["schedule_type"]
    schedule_value = arguments["schedule_value"]

    validation_error = _validate_schedule(schedule_type, schedule_value)
    if validation_error:
        return validation_error

    if task_type == "host":
        payload = _host_task_payload(arguments, schedule_type, schedule_value)
        filename, _request_id = _ipc.write_request_file("schedule_host_job", payload, reply_to=None)
        message = (
            f"Host job scheduled ({filename}): {arguments['name']} - "
            f"{schedule_type} - {schedule_value}"
        )
        return _scheduled_text(message)

    payload = _agent_task_payload(arguments, schedule_type, schedule_value)
    filename, _request_id = _ipc.write_request_file("schedule_task", payload, reply_to=None)
    return _scheduled_text(f"Task scheduled ({filename}): {schedule_type} - {schedule_value}")


def _task_type(arguments: dict[str, Any]) -> str:
    task_type = arguments.get("task_type", "agent")
    return str(task_type)


def _validate_task_type(task_type: str) -> CallToolResult | None:
    if task_type in ("agent", "host"):
        return None
    return tool_error(f'Invalid task_type: "{task_type}". Must be "agent" or "host".')


def _host_task_permission_error(task_type: str) -> CallToolResult | None:
    if task_type != "host" or _ipc.get_agent_tool_runtime().is_admin:
        return None
    return tool_error("Only the admin group can schedule host-level jobs.")


def _required_task_field_error(arguments: dict[str, Any], task_type: str) -> CallToolResult | None:
    if task_type == "agent":
        if arguments.get("prompt"):
            return None
        return tool_error('Agent tasks require a "prompt" field.')

    if not arguments.get("command"):
        return tool_error('Host tasks require a "command" field.')
    if not arguments.get("name"):
        return tool_error('Host tasks require a "name" field.')
    return None


def _host_task_payload(
    arguments: dict[str, Any], schedule_type: str, schedule_value: str
) -> dict[str, Any]:
    return {
        "name": arguments["name"],
        "command": arguments["command"],
        "schedule_type": schedule_type,
        "schedule_value": schedule_value,
        "cwd": arguments.get("cwd"),
        "timeout_seconds": arguments.get("timeout_seconds", 600),
        "createdBy": _ipc.get_agent_tool_runtime().group_folder,
    }


def _agent_target_group(arguments: dict[str, Any]) -> str:
    runtime = _ipc.get_agent_tool_runtime()
    return (arguments.get("target_group") if runtime.is_admin else None) or runtime.group_folder


def _agent_task_payload(
    arguments: dict[str, Any], schedule_type: str, schedule_value: str
) -> dict[str, Any]:
    return {
        "prompt": arguments["prompt"],
        "schedule_type": schedule_type,
        "schedule_value": schedule_value,
        "targetGroup": _agent_target_group(arguments),
        "context_mode": arguments.get("context_mode", "isolated"),
        "createdBy": _ipc.get_agent_tool_runtime().group_folder,
    }


def _scheduled_text(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=message)]


def _validate_schedule(schedule_type: str, schedule_value: str) -> CallToolResult | None:
    """Return a CallToolResult error if validation fails, else None."""
    if schedule_type == "cron":
        try:
            croniter(schedule_value)
        except (ValueError, KeyError):
            return tool_error(
                f'Invalid cron: "{schedule_value}". '
                'Use format like "0 9 * * *" (daily 9am) or "*/5 * * * *" (every 5 min).'
            )

    elif schedule_type == "interval":
        try:
            ms = int(schedule_value)
            if ms <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return tool_error(
                f'Invalid interval: "{schedule_value}". '
                'Must be positive milliseconds (e.g., "300000" for 5 min).'
            )

    elif schedule_type == "once":
        try:
            datetime.fromisoformat(schedule_value)
        except (ValueError, TypeError):
            return tool_error(
                f'Invalid timestamp: "{schedule_value}". '
                'Use ISO 8601 format like "2026-02-01T15:30:00".'
            )

    return None


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


async def _list_tasks_handle(  # async tool API.
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


# schedule_task keeps the explicit form — its definition is large enough to
# read better as a named function than inline in a decorator.
register(
    "schedule_task",
    ToolEntry(definition=_schedule_task_definition, handler=_schedule_task_handle),
)
register(
    "list_tasks",
    ToolEntry(definition=_list_tasks_definition, handler=_list_tasks_handle),
)
