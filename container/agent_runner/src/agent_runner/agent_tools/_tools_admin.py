"""Admin tools: register_group, deploy_changes (admin-only)."""

from __future__ import annotations

import os
import subprocess

from mcp.types import CallToolResult, TextContent, Tool

from agent_runner.agent_tools import _ipc
from agent_runner.agent_tools._registry import ToolEntry, register, tool_error

# -- register_group --


def _register_group_definition() -> Tool | None:
    if not _ipc.is_admin:
        return None
    return Tool(
        name="register_group",
        description=(
            "Register a chat group so the agent can "
            "respond to messages there. Admin group only.\n\n"
            "Use available_groups.json to find the JID for a "
            "group. The folder name should be lowercase with "
            'hyphens (e.g., "family-chat").'
        ),
        inputSchema={
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
    )


async def _register_group_handle(arguments: dict) -> list[TextContent] | CallToolResult:
    if not _ipc.is_admin:
        return tool_error("Only the admin group can register new groups.")

    data = {
        "type": "register_group",
        "jid": arguments["jid"],
        "name": arguments["name"],
        "folder": arguments["folder"],
        "trigger": arguments["trigger"],
        "timestamp": _ipc.now_iso(),
    }
    _ipc.write_ipc_file(_ipc.TASKS_DIR, data)
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


def _deploy_changes_definition() -> Tool | None:
    if not _ipc.is_admin:
        return None
    return Tool(
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
                    "default": "Deploy complete. Verifying service health.",
                    "description": "Prompt injected after restart to resume your conversation",
                },
            },
        },
    )


async def _deploy_changes_handle(arguments: dict) -> list[TextContent] | CallToolResult:
    if not _ipc.is_admin:
        return tool_error("Only the admin group can deploy.")

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
        "rebuildContainer": arguments.get("rebuild_container", False),
        "resumePrompt": arguments.get(
            "resume_prompt",
            "Deploy complete. Verifying service health.",
        ),
        "headSha": head_sha,
        "sessionId": session_id,
        "chatJid": _ipc.chat_jid,
        "timestamp": _ipc.now_iso(),
    }
    _ipc.write_ipc_file(_ipc.TASKS_DIR, data)
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


register(
    "register_group",
    ToolEntry(definition=_register_group_definition, handler=_register_group_handle),
)
register(
    "deploy_changes",
    ToolEntry(definition=_deploy_changes_definition, handler=_deploy_changes_handle),
)
