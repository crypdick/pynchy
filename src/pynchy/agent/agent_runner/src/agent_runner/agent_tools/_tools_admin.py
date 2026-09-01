"""Admin tools: register_group, deploy_changes (admin-only)."""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 - deploy helper uses fixed no-shell git argv.
from typing import Any

from mcp.types import CallToolResult, TextContent

from agent_runner.paths import PYNCHY_SOURCE

from . import _ipc
from ._registry import tool, tool_error

# -- register_group --


@tool(
    "register_group",
    (
        "Register a chat group so the agent can "
        "respond to messages there. Admin group only.\n\n"
        "Use available_groups.json to find the JID for a "
        "group. The folder name should be lowercase with "
        'hyphens (e.g., "family-chat").'
    ),
    {
        "type": "object",
        "properties": {
            "jid": {
                "type": "string",
                "description": "The group JID from available_groups.json",
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
    visible=lambda: _ipc.get_agent_tool_runtime().is_admin,
)
async def _register_group_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    if not _ipc.get_agent_tool_runtime().is_admin:
        return tool_error("Only the admin group can register new groups.")

    payload = {
        "jid": arguments["jid"],
        "name": arguments["name"],
        "folder": arguments["folder"],
        "trigger": arguments["trigger"],
    }
    _ipc.write_request_file("register_group", payload, reply_to=None)
    return [
        TextContent(
            type="text",
            text=(
                f'Group "{arguments["name"]}" registered. '
                "It will start receiving messages immediately."
            ),
        )
    ]


# -- deploy_changes --


@tool(
    "deploy_changes",
    (
        "Deploy committed code changes to the running "
        "pynchy service. Optionally rebuilds the container "
        "image, then restarts the service. Your conversation "
        "resumes automatically after restart. Commit your "
        "changes with git before calling this. Always run "
        "tests before deploying."
    ),
    {
        "type": "object",
        "properties": {
            "rebuild_container": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true only if src/pynchy/agent/Dockerfile or "
                    "src/pynchy/agent/entrypoint.sh changed. "
                    "Code/dependency changes use false (default)."
                ),
            },
            "resume_prompt": {
                "type": "string",
                "default": "Deploy complete. Verifying service health.",
                "description": "Prompt injected after restart to resume your conversation",
            },
        },
    },
    visible=lambda: _ipc.get_agent_tool_runtime().is_admin,
)
async def _deploy_changes_handle(arguments: dict[str, Any]) -> list[TextContent] | CallToolResult:
    if not _ipc.get_agent_tool_runtime().is_admin:
        return tool_error("Only the admin group can deploy.")

    try:
        head_sha = (
            await asyncio.to_thread(
                subprocess.run,
                [
                    "git",
                    "rev-parse",
                    "HEAD",
                ],  # git is a trusted workspace executable; no shell or user-controlled argv.
                cwd=PYNCHY_SOURCE,
                capture_output=True,
                text=True,
                check=True,
            )
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        head_sha = ""

    session_id = os.environ.get("PYNCHY_SESSION_ID", "")

    payload = {
        "rebuildContainer": arguments.get("rebuild_container", False),
        "resumePrompt": arguments.get(
            "resume_prompt",
            "Deploy complete. Verifying service health.",
        ),
        "headSha": head_sha,
        "sessionId": session_id,
        "chatJid": _ipc.get_agent_tool_runtime().chat_jid,
    }
    _ipc.write_request_file("deploy", payload, reply_to=None)
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
