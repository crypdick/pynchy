"""Task scheduling and management tools: schedule_task, list_tasks, pause/resume/cancel."""

from __future__ import annotations

import json

from croniter import croniter
from mcp.types import CallToolResult, TextContent, Tool

from agent_runner.agent_tools import _ipc
from agent_runner.agent_tools._registry import ToolEntry, register

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
            "CONTEXT MODE (agent tasks only) - Choose based on task type:\n"
            '\u2022 "group": Task runs in the group\'s conversation '
            "context, with access to chat history. Use for tasks "
            "that need context about ongoing discussions, user "
            "preferences, or recent interactions.\n"
            '\u2022 "isolated": Task runs in a fresh session with no '
            "conversation history. Use for independent tasks that "
            "don't need prior context. When using isolated mode, "
            "include all necessary context in the prompt itself.\n\n"
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
                "context_mode": {
                    "type": "string",
                    "enum": ["group", "isolated"],
                    "default": "group",
                    "description": (
                        "Agent tasks only: group=runs with chat history, isolated=fresh session"
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


async def _schedule_task_handle(arguments: dict) -> list[TextContent] | CallToolResult:
    task_type = arguments.get("task_type", "agent")

    if task_type not in ("agent", "host"):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f'Invalid task_type: "{task_type}". Must be "agent" or "host".',
                )
            ],
            isError=True,
        )

    if task_type == "host" and not _ipc.is_admin:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="Only the admin group can schedule host-level jobs.",
                )
            ],
            isError=True,
        )

    if task_type == "agent":
        if not arguments.get("prompt"):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='Agent tasks require a "prompt" field.',
                    )
                ],
                isError=True,
            )
    else:
        if not arguments.get("command"):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='Host tasks require a "command" field.',
                    )
                ],
                isError=True,
            )
        if not arguments.get("name"):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='Host tasks require a "name" field.',
                    )
                ],
                isError=True,
            )

    schedule_type = arguments["schedule_type"]
    schedule_value = arguments["schedule_value"]

    validation_error = _validate_schedule(schedule_type, schedule_value)
    if validation_error:
        return validation_error

    if task_type == "host":
        data = {
            "type": "schedule_host_job",
            "name": arguments["name"],
            "command": arguments["command"],
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "cwd": arguments.get("cwd"),
            "timeout_seconds": arguments.get("timeout_seconds", 600),
            "createdBy": _ipc.group_folder,
            "timestamp": _ipc.now_iso(),
        }
        filename = _ipc.write_ipc_file(_ipc.TASKS_DIR, data)
        return [
            TextContent(
                type="text",
                text=(
                    f"Host job scheduled ({filename}): {arguments['name']} - "
                    f"{schedule_type} - {schedule_value}"
                ),
            )
        ]

    target_group = (arguments.get("target_group") if _ipc.is_admin else None) or _ipc.group_folder

    data = {
        "type": "schedule_task",
        "prompt": arguments["prompt"],
        "schedule_type": schedule_type,
        "schedule_value": schedule_value,
        "context_mode": arguments.get("context_mode", "group"),
        "targetGroup": target_group,
        "createdBy": _ipc.group_folder,
        "timestamp": _ipc.now_iso(),
    }

    filename = _ipc.write_ipc_file(_ipc.TASKS_DIR, data)
    return [
        TextContent(
            type="text",
            text=f"Task scheduled ({filename}): {schedule_type} - {schedule_value}",
        )
    ]


def _validate_schedule(schedule_type: str, schedule_value: str) -> CallToolResult | None:
    """Return a CallToolResult error if validation fails, else None."""
    if schedule_type == "cron":
        try:
            croniter(schedule_value)
        except (ValueError, KeyError):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f'Invalid cron: "{schedule_value}". '
                            "Use format like "
                            '"0 9 * * *" (daily 9am) or '
                            '"*/5 * * * *" (every 5 min).'
                        ),
                    )
                ],
                isError=True,
            )

    elif schedule_type == "interval":
        try:
            ms = int(schedule_value)
            if ms <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f'Invalid interval: "{schedule_value}".'
                            " Must be positive milliseconds "
                            '(e.g., "300000" for 5 min).'
                        ),
                    )
                ],
                isError=True,
            )

    elif schedule_type == "once":
        from datetime import datetime

        try:
            datetime.fromisoformat(schedule_value)
        except (ValueError, TypeError):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f'Invalid timestamp: "{schedule_value}"'
                            ". Use ISO 8601 format like "
                            '"2026-02-01T15:30:00".'
                        ),
                    )
                ],
                isError=True,
            )

    return None


# -- list_tasks --


def _list_tasks_definition() -> Tool:
    return Tool(
        name="list_tasks",
        description=(
            "List all scheduled tasks (both agent tasks and host "
            "jobs). Each entry is labelled [agent] or [host]. "
            "From admin: shows all tasks across all groups. "
            "From other groups: shows only that group's agent tasks."
        ),
        inputSchema={"type": "object", "properties": {}},
    )


async def _list_tasks_handle(arguments: dict) -> list[TextContent]:
    tasks_file = _ipc.IPC_DIR / "current_tasks.json"

    try:
        if not tasks_file.exists():
            return [TextContent(type="text", text="No scheduled tasks found.")]

        all_tasks = json.loads(tasks_file.read_text())
        tasks = (
            all_tasks
            if _ipc.is_admin
            else [t for t in all_tasks if t.get("groupFolder") == _ipc.group_folder]
        )

        if not tasks:
            return [TextContent(type="text", text="No scheduled tasks found.")]

        lines = []
        for t in tasks:
            task_type = t.get("type", "agent")
            if task_type == "host":
                label = t.get("name") or t.get("command", "")[:50]
                lines.append(
                    f"- [{t['id']}] [host] {label} "
                    f"({t['schedule_type']}: {t['schedule_value']}) "
                    f"- {t['status']}, "
                    f"next: {t.get('next_run', 'N/A')}"
                )
            else:
                prompt = t.get("prompt", "")[:50]
                lines.append(
                    f"- [{t['id']}] [agent] {prompt}... "
                    f"({t['schedule_type']}: {t['schedule_value']}) "
                    f"- {t['status']}, "
                    f"next: {t.get('next_run', 'N/A')}"
                )

        return [
            TextContent(
                type="text",
                text=f"Scheduled tasks:\n{chr(10).join(lines)}",
            )
        ]

    except Exception as exc:
        return [TextContent(type="text", text=f"Error reading tasks: {exc}")]


# -- pause/resume/cancel --


def _pause_task_definition() -> Tool:
    return Tool(
        name="pause_task",
        description="Pause a scheduled task or host job. It will not run until resumed.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to pause",
                },
            },
            "required": ["task_id"],
        },
    )


def _resume_task_definition() -> Tool:
    return Tool(
        name="resume_task",
        description="Resume a paused task or host job.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to resume",
                },
            },
            "required": ["task_id"],
        },
    )


def _cancel_task_definition() -> Tool:
    return Tool(
        name="cancel_task",
        description="Cancel and delete a scheduled task or host job.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to cancel",
                },
            },
            "required": ["task_id"],
        },
    )


def _task_action(action: str, task_id: str) -> list[TextContent]:
    """Write a pause/resume/cancel IPC file and return confirmation."""
    _ipc.write_ipc_file(
        _ipc.TASKS_DIR,
        {
            "type": action,
            "taskId": task_id,
            "groupFolder": _ipc.group_folder,
            "isAdmin": _ipc.is_admin,
            "timestamp": _ipc.now_iso(),
        },
    )
    verb = action.replace("_task", "")
    if verb == "cancel":
        verb = "cancellation"
    return [TextContent(type="text", text=f"Task {task_id} {verb} requested.")]


async def _pause_task_handle(arguments: dict) -> list[TextContent]:
    return _task_action("pause_task", arguments["task_id"])


async def _resume_task_handle(arguments: dict) -> list[TextContent]:
    return _task_action("resume_task", arguments["task_id"])


async def _cancel_task_handle(arguments: dict) -> list[TextContent]:
    return _task_action("cancel_task", arguments["task_id"])


register(
    "schedule_task",
    ToolEntry(definition=_schedule_task_definition, handler=_schedule_task_handle),
)
register(
    "list_tasks",
    ToolEntry(definition=_list_tasks_definition, handler=_list_tasks_handle),
)
register(
    "pause_task",
    ToolEntry(definition=_pause_task_definition, handler=_pause_task_handle),
)
register(
    "resume_task",
    ToolEntry(definition=_resume_task_definition, handler=_resume_task_handle),
)
register(
    "cancel_task",
    ToolEntry(definition=_cancel_task_definition, handler=_cancel_task_handle),
)
