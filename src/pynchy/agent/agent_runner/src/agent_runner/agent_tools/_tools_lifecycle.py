"""Lifecycle tools: reset_context, finished_work, sync_worktree_to_main."""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from agent_runner.agent_tools import _ipc
from agent_runner.agent_tools._registry import tool, tool_error

# -- sync_worktree_to_main --


@tool(
    "sync_worktree_to_main",
    (
        "Publish your committed changes. Depending on workspace "
        "policy, this either merges into main and pushes, or "
        "pushes to a branch and opens/updates a PR. "
        "Commit all changes first. On conflict, your worktree "
        "will have conflict markers — fix them, git add, "
        "git rebase --continue, then retry."
    ),
    {"type": "object", "properties": {}},
)
async def _sync_worktree_handle(arguments: dict) -> list[TextContent] | CallToolResult:
    request_id = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}"
    _ipc.write_ipc_file(
        _ipc.TASKS_DIR,
        {
            "type": "sync_worktree_to_main",
            "groupFolder": _ipc.group_folder,
            "requestId": request_id,
            "timestamp": _ipc.now_iso(),
        },
    )

    result_file = _ipc.IPC_DIR / "merge_results" / f"{request_id}.json"
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
            return tool_error(result["message"])
        await asyncio.sleep(0.3)

    return tool_error("Timed out (120s). Retry or check with the host.")


def _exit_container() -> None:
    """Write the close sentinel and terminate the container process."""
    close_sentinel = Path("/workspace/ipc/input/_close")
    close_sentinel.parent.mkdir(parents=True, exist_ok=True)
    close_sentinel.write_text("")
    os._exit(0)


# -- finished_work --


@tool(
    "finished_work",
    (
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
    {"type": "object", "properties": {}},
    visible=lambda: _ipc.is_scheduled_task,
)
async def _finished_work_handle(arguments: dict) -> list[TextContent]:
    _ipc.write_ipc_file(
        _ipc.TASKS_DIR,
        {
            "type": "finished_work",
            "groupFolder": _ipc.group_folder,
            "chatJid": _ipc.chat_jid,
            "timestamp": _ipc.now_iso(),
        },
    )
    _exit_container()


# -- reset_context --


@tool(
    "reset_context",
    (
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
    {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "Message for your next session. Include "
                    "all context needed to continue the task. "
                    "Only set this if there is actually pending "
                    "work or context to hand off. If there is "
                    "nothing to hand off (e.g. the user just "
                    "asked to clear context), omit this "
                    "parameter entirely — do NOT invent a "
                    "message."
                ),
            },
        },
    },
)
async def _reset_context_handle(arguments: dict) -> list[TextContent]:
    data: dict[str, str] = {
        "type": "reset_context",
        "chatJid": _ipc.chat_jid,
        "groupFolder": _ipc.group_folder,
        "timestamp": _ipc.now_iso(),
    }
    if arguments.get("message"):
        data["message"] = arguments["message"]
    _ipc.write_ipc_file(_ipc.TASKS_DIR, data)
    _exit_container()
