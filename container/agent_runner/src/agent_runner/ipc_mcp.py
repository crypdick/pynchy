"""Stdio MCP Server for Pynchy.

Port of container/agent-runner/src/ipc-mcp-stdio.ts.
Standalone process that agent teams subagents can inherit.
Reads context from environment variables, writes IPC files for the host.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from croniter import croniter
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"

# Context from environment variables (set by the agent runner)
chat_jid = os.environ.get("PYNCHY_CHAT_JID", "")
group_folder = os.environ.get("PYNCHY_GROUP_FOLDER", "")
is_main = os.environ.get("PYNCHY_IS_MAIN") == "1"


def write_ipc_file(directory: Path, data: dict) -> str:
    """Write an IPC file atomically (temp file + rename)."""
    directory.mkdir(parents=True, exist_ok=True)

    filename = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}.json"
    filepath = directory / filename

    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    return filename


server = Server("pynchy")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="send_message",
            description=(
                "Send a message to the user or group immediately while you're still running. "
                "Use this for progress updates or to send multiple messages. You can call this "
                "multiple times. Note: when running as a scheduled task, your final output is NOT "
                "sent to the user — use this tool if you need to communicate with the user or group."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message text to send",
                    },
                    "sender": {
                        "type": "string",
                        "description": (
                            'Your role/identity name (e.g. "Researcher"). '
                            "When set, messages appear from a dedicated bot in Telegram."
                        ),
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="schedule_task",
            description=(
                "Schedule a recurring or one-time task. The task will run as a full agent "
                "with access to all tools.\n\n"
                "CONTEXT MODE - Choose based on task type:\n"
                "\u2022 \"group\": Task runs in the group's conversation context, with access to "
                "chat history. Use for tasks that need context about ongoing discussions, "
                "user preferences, or recent interactions.\n"
                "\u2022 \"isolated\": Task runs in a fresh session with no conversation history. "
                "Use for independent tasks that don't need prior context. When using isolated "
                "mode, include all necessary context in the prompt itself.\n\n"
                "If unsure which mode to use, you can ask the user. Examples:\n"
                "- \"Remind me about our discussion\" \u2192 group (needs conversation context)\n"
                "- \"Check the weather every morning\" \u2192 isolated (self-contained task)\n"
                "- \"Follow up on my request\" \u2192 group (needs to know what was requested)\n"
                "- \"Generate a daily report\" \u2192 isolated (just needs instructions in prompt)\n\n"
                "MESSAGING BEHAVIOR - The task agent's output is sent to the user or group. "
                "It can also use send_message for immediate delivery, or wrap output in "
                "<internal> tags to suppress it. Include guidance in the prompt about whether "
                "the agent should:\n"
                "\u2022 Always send a message (e.g., reminders, daily briefings)\n"
                "\u2022 Only send a message when there's something to report (e.g., \"notify me if...\")\n"
                "\u2022 Never send a message (background maintenance tasks)\n\n"
                "SCHEDULE VALUE FORMAT (all times are LOCAL timezone):\n"
                "\u2022 cron: Standard cron expression (e.g., \"*/5 * * * *\" for every 5 minutes, "
                "\"0 9 * * *\" for daily at 9am LOCAL time)\n"
                "\u2022 interval: Milliseconds between runs (e.g., \"300000\" for 5 minutes, "
                "\"3600000\" for 1 hour)\n"
                "\u2022 once: Local time WITHOUT \"Z\" suffix (e.g., \"2026-02-01T15:30:00\"). "
                "Do NOT use UTC/Z suffix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "What the agent should do when the task runs. "
                            "For isolated mode, include all necessary context here."
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
                            'cron: "*/5 * * * *" | interval: milliseconds like '
                            '"300000" | once: local timestamp like '
                            '"2026-02-01T15:30:00" (no Z suffix!)'
                        ),
                    },
                    "context_mode": {
                        "type": "string",
                        "enum": ["group", "isolated"],
                        "default": "group",
                        "description": (
                            "group=runs with chat history and memory, "
                            "isolated=fresh session (include context in prompt)"
                        ),
                    },
                    "target_group_jid": {
                        "type": "string",
                        "description": "(Main group only) JID of the group to schedule the task for. Defaults to the current group.",
                    },
                },
                "required": ["prompt", "schedule_type", "schedule_value"],
            },
        ),
        Tool(
            name="list_tasks",
            description=(
                "List all scheduled tasks. From main: shows all tasks. "
                "From other groups: shows only that group's tasks."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="pause_task",
            description="Pause a scheduled task. It will not run until resumed.",
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
        ),
        Tool(
            name="resume_task",
            description="Resume a paused task.",
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
        ),
        Tool(
            name="cancel_task",
            description="Cancel and delete a scheduled task.",
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
        ),
        Tool(
            name="register_group",
            description=(
                "Register a new WhatsApp group so the agent can respond to messages there. "
                "Main group only.\n\n"
                "Use available_groups.json to find the JID for a group. "
                'The folder name should be lowercase with hyphens (e.g., "family-chat").'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jid": {
                        "type": "string",
                        "description": 'The WhatsApp JID (e.g., "120363336345536173@g.us")',
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the group",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder name for group files (lowercase, hyphens)",
                    },
                    "trigger": {
                        "type": "string",
                        "description": 'Trigger word (e.g., "@Pynchy")',
                    },
                },
                "required": ["jid", "name", "folder", "trigger"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent] | CallToolResult:
    match name:
        case "send_message":
            data = {
                "type": "message",
                "chatJid": chat_jid,
                "text": arguments["text"],
                "groupFolder": group_folder,
                "timestamp": _now_iso(),
            }
            if arguments.get("sender"):
                data["sender"] = arguments["sender"]

            write_ipc_file(MESSAGES_DIR, data)
            return [TextContent(type="text", text="Message sent.")]

        case "schedule_task":
            # Validate schedule_value
            schedule_type = arguments["schedule_type"]
            schedule_value = arguments["schedule_value"]

            if schedule_type == "cron":
                try:
                    croniter(schedule_value)
                except (ValueError, KeyError):
                    return CallToolResult(
                        content=[TextContent(
                            type="text",
                            text=f'Invalid cron: "{schedule_value}". '
                            'Use format like "0 9 * * *" (daily 9am) or "*/5 * * * *" (every 5 min).',
                        )],
                        isError=True,
                    )

            elif schedule_type == "interval":
                try:
                    ms = int(schedule_value)
                    if ms <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    return CallToolResult(
                        content=[TextContent(
                            type="text",
                            text=f'Invalid interval: "{schedule_value}". '
                            'Must be positive milliseconds (e.g., "300000" for 5 min).',
                        )],
                        isError=True,
                    )

            elif schedule_type == "once":
                from datetime import datetime
                try:
                    datetime.fromisoformat(schedule_value)
                except (ValueError, TypeError):
                    return CallToolResult(
                        content=[TextContent(
                            type="text",
                            text=f'Invalid timestamp: "{schedule_value}". '
                            'Use ISO 8601 format like "2026-02-01T15:30:00".',
                        )],
                        isError=True,
                    )

            # Non-main groups can only schedule for themselves
            target_jid = (
                arguments.get("target_group_jid") if is_main else None
            ) or chat_jid

            data = {
                "type": "schedule_task",
                "prompt": arguments["prompt"],
                "schedule_type": schedule_type,
                "schedule_value": schedule_value,
                "context_mode": arguments.get("context_mode", "group"),
                "targetJid": target_jid,
                "createdBy": group_folder,
                "timestamp": _now_iso(),
            }

            filename = write_ipc_file(TASKS_DIR, data)
            return [TextContent(
                type="text",
                text=f"Task scheduled ({filename}): {schedule_type} - {schedule_value}",
            )]

        case "list_tasks":
            tasks_file = IPC_DIR / "current_tasks.json"

            try:
                if not tasks_file.exists():
                    return [TextContent(type="text", text="No scheduled tasks found.")]

                all_tasks = json.loads(tasks_file.read_text())
                tasks = (
                    all_tasks
                    if is_main
                    else [t for t in all_tasks if t.get("groupFolder") == group_folder]
                )

                if not tasks:
                    return [TextContent(type="text", text="No scheduled tasks found.")]

                formatted = "\n".join(
                    f"- [{t['id']}] {t['prompt'][:50]}... "
                    f"({t['schedule_type']}: {t['schedule_value']}) - "
                    f"{t['status']}, next: {t.get('next_run', 'N/A')}"
                    for t in tasks
                )
                return [TextContent(type="text", text=f"Scheduled tasks:\n{formatted}")]

            except Exception as exc:
                return [TextContent(type="text", text=f"Error reading tasks: {exc}")]

        case "pause_task":
            data = {
                "type": "pause_task",
                "taskId": arguments["task_id"],
                "groupFolder": group_folder,
                "isMain": is_main,
                "timestamp": _now_iso(),
            }
            write_ipc_file(TASKS_DIR, data)
            return [TextContent(
                type="text",
                text=f"Task {arguments['task_id']} pause requested.",
            )]

        case "resume_task":
            data = {
                "type": "resume_task",
                "taskId": arguments["task_id"],
                "groupFolder": group_folder,
                "isMain": is_main,
                "timestamp": _now_iso(),
            }
            write_ipc_file(TASKS_DIR, data)
            return [TextContent(
                type="text",
                text=f"Task {arguments['task_id']} resume requested.",
            )]

        case "cancel_task":
            data = {
                "type": "cancel_task",
                "taskId": arguments["task_id"],
                "groupFolder": group_folder,
                "isMain": is_main,
                "timestamp": _now_iso(),
            }
            write_ipc_file(TASKS_DIR, data)
            return [TextContent(
                type="text",
                text=f"Task {arguments['task_id']} cancellation requested.",
            )]

        case "register_group":
            if not is_main:
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text="Only the main group can register new groups.",
                    )],
                    isError=True,
                )

            data = {
                "type": "register_group",
                "jid": arguments["jid"],
                "name": arguments["name"],
                "folder": arguments["folder"],
                "trigger": arguments["trigger"],
                "timestamp": _now_iso(),
            }
            write_ipc_file(TASKS_DIR, data)
            return [TextContent(
                type="text",
                text=f"Group \"{arguments['name']}\" registered. "
                "It will start receiving messages immediately.",
            )]

        case _:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_server())
