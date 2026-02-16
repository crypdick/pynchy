"""Stdio MCP Server for Pynchy.

Standalone process that agent teams subagents can inherit.
Reads context from environment variables, writes IPC files for the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
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
is_god = os.environ.get("PYNCHY_IS_GOD") == "1"
is_scheduled_task = os.environ.get("PYNCHY_IS_SCHEDULED_TASK") == "1"


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
    tools = [
        Tool(
            name="send_message",
            description=(
                "Send a message to the user or group immediately while "
                "you're still running. Use this for progress updates or "
                "to send multiple messages. You can call this multiple "
                "times. Note: when running as a scheduled task, your "
                "final output is NOT sent to the user — use this tool "
                "if you need to communicate with the user or group."
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
                            "When set, messages appear from a dedicated "
                            "bot in Telegram."
                        ),
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="schedule_task",
            description=(
                "Schedule a recurring or one-time task.\n\n"
                "TASK TYPES:\n"
                '\u2022 "agent" (default): Runs a full agent with access '
                "to all tools in a container. Use for tasks requiring "
                "reasoning, tool use, or user interaction.\n"
                '\u2022 "host" (god group only): Runs a shell command '
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
                            "agent=containerized LLM task, host=shell command (god only)"
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
                    "target_group_jid": {
                        "type": "string",
                        "description": (
                            "(God group only) JID of the group to "
                            "schedule the task for. Defaults to the "
                            "current group."
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
                        "description": ("Host tasks only: Command timeout in seconds."),
                    },
                },
                "required": ["schedule_type", "schedule_value"],
            },
        ),
        Tool(
            name="list_tasks",
            description=(
                "List all scheduled tasks. From god: shows all tasks. "
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
                "Register a new WhatsApp group so the agent can "
                "respond to messages there. God group only.\n\n"
                "Use available_groups.json to find the JID for a "
                "group. The folder name should be lowercase with "
                'hyphens (e.g., "family-chat").'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jid": {
                        "type": "string",
                        "description": ('The WhatsApp JID (e.g., "120363336345536173@g.us")'),
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the group",
                    },
                    "folder": {
                        "type": "string",
                        "description": ("Folder name for group files (lowercase, hyphens)"),
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

    tools.append(
        Tool(
            name="sync_worktree_to_main",
            description=(
                "Merge your worktree into main and push to origin. "
                "Commit all changes first. On conflict, your worktree "
                "will have conflict markers — fix them, git add, "
                "git rebase --continue, then retry."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    )

    if is_scheduled_task:
        tools.append(
            Tool(
                name="finished_work",
                description=(
                    "Signal that your scheduled task is complete and shut "
                    "down this container. This will:\n"
                    "1. Merge any un-synced worktree commits (safety net)\n"
                    "2. Notify the chat that the task finished\n"
                    "3. Terminate this container\n\n"
                    "Call sync_worktree_to_main first if you have commits "
                    "to push. This tool is a final safety net — it will "
                    "merge anything you missed.\n\n"
                    "After calling this tool, the container exits "
                    "immediately. Do NOT attempt further work."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
        )

    tools.append(
        Tool(
            name="reset_context",
            description=(
                "Reset your conversation context and start a fresh "
                "session. Use this when your context has grown large "
                "and you want to continue with a clean slate. You "
                "can pass a message to your future self — e.g. a "
                "plan, summary, or instructions — which becomes the "
                "initial prompt of the new session.\n\n"
                "After calling this tool, the current session ends "
                "immediately. Do NOT attempt further work after "
                "calling it.\n\n"
                "Wrap any user-facing confirmation text in <host> "
                "tags so it displays as a host message, e.g.:\n"
                "<host>Context cleared. Starting fresh session.</host>"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "Message for your next session. Include "
                            "all context needed to continue the task."
                        ),
                    },
                },
                "required": ["message"],
            },
        ),
    )

    if is_god:
        tools.append(
            Tool(
                name="deploy_changes",
                description=(
                    "Deploy committed code changes to the running "
                    "pynchy service. Optionally rebuilds the container "
                    "image, then restarts the service. Your conversation "
                    "resumes automatically after restart. Commit your "
                    "changes with git before calling this. Always run "
                    "tests before deploying."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "rebuild_container": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Set true only if container/Dockerfile or "
                                "container/entrypoint.sh changed. "
                                "Code/dependency changes use false (default)."
                            ),
                        },
                        "resume_prompt": {
                            "type": "string",
                            "default": ("Deploy complete. Verifying service health."),
                            "description": (
                                "Prompt injected after restart to resume your conversation"
                            ),
                        },
                    },
                },
            ),
        )

    return tools


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
            task_type = arguments.get("task_type", "agent")

            # Validate task_type
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

            # Host jobs are god-only
            if task_type == "host" and not is_god:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text="Only the god group can schedule host-level jobs.",
                        )
                    ],
                    isError=True,
                )

            # Validate required fields based on task_type
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
            else:  # host
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

            # Validate schedule_value
            schedule_type = arguments["schedule_type"]
            schedule_value = arguments["schedule_value"]

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

            # Route to appropriate handler
            if task_type == "host":
                data = {
                    "type": "schedule_host_job",
                    "name": arguments["name"],
                    "command": arguments["command"],
                    "schedule_type": schedule_type,
                    "schedule_value": schedule_value,
                    "cwd": arguments.get("cwd"),
                    "timeout_seconds": arguments.get("timeout_seconds", 600),
                    "createdBy": group_folder,
                    "timestamp": _now_iso(),
                }
                filename = write_ipc_file(TASKS_DIR, data)
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"Host job scheduled ({filename}): {arguments['name']} - "
                            f"{schedule_type} - {schedule_value}"
                        ),
                    )
                ]

            # Agent task (existing logic)
            # Non-god groups can only schedule for themselves
            target_jid = (arguments.get("target_group_jid") if is_god else None) or chat_jid

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
            return [
                TextContent(
                    type="text",
                    text=(f"Task scheduled ({filename}): {schedule_type} - {schedule_value}"),
                )
            ]

        case "list_tasks":
            tasks_file = IPC_DIR / "current_tasks.json"

            try:
                if not tasks_file.exists():
                    return [
                        TextContent(
                            type="text",
                            text="No scheduled tasks found.",
                        )
                    ]

                all_tasks = json.loads(tasks_file.read_text())
                tasks = (
                    all_tasks
                    if is_god
                    else [t for t in all_tasks if t.get("groupFolder") == group_folder]
                )

                if not tasks:
                    return [
                        TextContent(
                            type="text",
                            text="No scheduled tasks found.",
                        )
                    ]

                formatted = "\n".join(
                    f"- [{t['id']}] {t['prompt'][:50]}... "
                    f"({t['schedule_type']}: {t['schedule_value']}) "
                    f"- {t['status']}, "
                    f"next: {t.get('next_run', 'N/A')}"
                    for t in tasks
                )
                return [
                    TextContent(
                        type="text",
                        text=f"Scheduled tasks:\n{formatted}",
                    )
                ]

            except Exception as exc:
                return [
                    TextContent(
                        type="text",
                        text=f"Error reading tasks: {exc}",
                    )
                ]

        case "pause_task" | "resume_task" | "cancel_task":
            return _task_action(name, arguments["task_id"])

        case "register_group":
            if not is_god:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=("Only the god group can register new groups."),
                        )
                    ],
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
            return [
                TextContent(
                    type="text",
                    text=(
                        f'Group "{arguments["name"]}" registered. '
                        "It will start receiving messages immediately."
                    ),
                )
            ]

        case "reset_context":
            data = {
                "type": "reset_context",
                "message": arguments["message"],
                "chatJid": chat_jid,
                "groupFolder": group_folder,
                "timestamp": _now_iso(),
            }
            write_ipc_file(TASKS_DIR, data)

            # Write close sentinel and exit immediately — no point
            # returning a response to an LLM that's about to be killed.
            close_sentinel = Path("/workspace/ipc/input/_close")
            close_sentinel.parent.mkdir(parents=True, exist_ok=True)
            close_sentinel.write_text("")
            os._exit(0)

        case "sync_worktree_to_main":
            request_id = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}"
            write_ipc_file(
                TASKS_DIR,
                {
                    "type": "sync_worktree_to_main",
                    "groupFolder": group_folder,
                    "requestId": request_id,
                    "timestamp": _now_iso(),
                },
            )

            # Block until host writes response
            result_file = IPC_DIR / "merge_results" / f"{request_id}.json"
            timeout = 120
            start = time.time()
            while time.time() - start < timeout:
                if result_file.exists():
                    try:
                        result = json.loads(result_file.read_text())
                        result_file.unlink()
                    except (json.JSONDecodeError, OSError):
                        await asyncio.sleep(0.3)
                        continue

                    if result.get("success"):
                        return [TextContent(type="text", text=result["message"])]
                    return CallToolResult(
                        content=[TextContent(type="text", text=result["message"])],
                        isError=True,
                    )
                await asyncio.sleep(0.3)

            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Timed out (120s). Retry or check with the host.",
                    )
                ],
                isError=True,
            )

        case "finished_work":
            write_ipc_file(
                TASKS_DIR,
                {
                    "type": "finished_work",
                    "groupFolder": group_folder,
                    "chatJid": chat_jid,
                    "timestamp": _now_iso(),
                },
            )

            # Write close sentinel and exit — same pattern as reset_context.
            close_sentinel = Path("/workspace/ipc/input/_close")
            close_sentinel.parent.mkdir(parents=True, exist_ok=True)
            close_sentinel.write_text("")
            os._exit(0)

        case "deploy_changes":
            if not is_god:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text="Only the god group can deploy.",
                        )
                    ],
                    isError=True,
                )

            # Read current HEAD for rollback reference
            try:
                head_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd="/workspace/project",
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            except subprocess.CalledProcessError:
                head_sha = ""

            session_id = os.environ.get("PYNCHY_SESSION_ID", "")

            data = {
                "type": "deploy",
                "rebuildContainer": arguments.get(
                    "rebuild_container",
                    False,
                ),
                "resumePrompt": arguments.get(
                    "resume_prompt",
                    "Deploy complete. Verifying service health.",
                ),
                "headSha": head_sha,
                "sessionId": session_id,
                "chatJid": chat_jid,
                "timestamp": _now_iso(),
            }
            write_ipc_file(TASKS_DIR, data)
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Deploy initiated (HEAD: {head_sha[:8]}). "
                        "The service will restart and resume this "
                        "conversation."
                    ),
                )
            ]

        case _:
            return [
                TextContent(
                    type="text",
                    text=f"Unknown tool: {name}",
                )
            ]


def _task_action(action: str, task_id: str) -> list[TextContent]:
    """Write a pause/resume/cancel IPC file and return confirmation."""
    write_ipc_file(
        TASKS_DIR,
        {
            "type": action,
            "taskId": task_id,
            "groupFolder": group_folder,
            "isGod": is_god,
            "timestamp": _now_iso(),
        },
    )
    # "pause_task" → "pause", "cancel_task" → "cancellation"
    verb = action.replace("_task", "")
    if verb == "cancel":
        verb = "cancellation"
    return [TextContent(type="text", text=f"Task {task_id} {verb} requested.")]


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_server())
