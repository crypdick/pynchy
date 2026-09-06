"""Lifecycle tools: reset_context and sync_worktree_to_main."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Any, NoReturn

from mcp.types import CallToolResult, TextContent

from . import _ipc
from ._registry import tool, tool_error

# -- sync_worktree_to_main --


def _publication_result_message(result: dict[str, Any]) -> str:
    """Prefer actionable per-repository diagnostics over an aggregate status."""
    repo_results = result.get("repos")
    messages = (
        [
            f"{slug}: {repo_result['message']}"
            for slug, repo_result in repo_results.items()
            if isinstance(slug, str)
            and isinstance(repo_result, dict)
            and isinstance(repo_result.get("message"), str)
            and repo_result["message"]
        ]
        if isinstance(repo_results, dict)
        else []
    )
    if messages:
        return "\n".join(messages)
    message = result.get("message")
    return str(message) if message else "Publication failed without a diagnostic."


async def _request_publication(
    kind: str, payload: dict[str, str]
) -> list[TextContent] | CallToolResult:
    """Send one PR-only publication request and wait for its host result."""
    request_id = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    _ipc.write_request_file(
        kind,
        payload,
        request_id=request_id,
        reply_to="merge_results",
    )

    result_file = _ipc.get_agent_tool_runtime().ipc_dir / "merge_results" / f"{request_id}.json"
    timeout = 120
    start = time.time()
    while time.time() - start < timeout:
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
                result_file.unlink()
            except (json.JSONDecodeError, OSError):
                await asyncio.sleep(0.3)
                continue

            if result.get("success"):
                return [TextContent(type="text", text=_publication_result_message(result))]
            return tool_error(_publication_result_message(result))
        await asyncio.sleep(0.3)

    return tool_error("Timed out (120s). Retry or check with the host.")


@tool(
    "sync_worktree_to_main",
    (
        "Publish committed workspace changes for review. The host pushes the isolated "
        "worktree branch and opens a pull request, returning its canonical URL. Write a "
        "review-ready title and Markdown body that explain the actual change, behavior, and "
        "checks run; never mention the workspace path or call the PR automated. For a Linear "
        "work item, the host derives its first branch as team-key/issue-number/title-slug "
        "and reuses that branch for subsequent publication even when the title changes. "
        "The host adds a `Resolves TEAM-123` footer so Linear treats the PR as closing work. "
        "After publication, the host attaches the canonical PR to the exact Linear issue. "
        "Include the returned URL in the work-item outcome evidence; do not attach it through "
        "a generic Linear tool. Resolve any reported conflict or publication failure and retry."
    ),
    {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "Concise, imperative pull-request title.",
            },
            "body": {
                "type": "string",
                "minLength": 1,
                "maxLength": 65536,
                "description": "Markdown review summary with behavior and checks run.",
            },
        },
        "required": ["title", "body"],
        "additionalProperties": False,
    },
)
async def _sync_worktree_handle(arguments: dict[str, Any]) -> list[TextContent] | CallToolResult:
    runtime = _ipc.get_agent_tool_runtime()
    payload = {
        "groupFolder": runtime.group_folder,
        "publication": "pull-request",
        "title": arguments["title"],
        "body": arguments["body"],
    }
    if runtime.turn_id:
        payload["turn_id"] = runtime.turn_id
    return await _request_publication(
        "sync_worktree_to_main",
        payload,
    )


@tool(
    "publish_managed_feature",
    (
        "Publish one committed managed new-feature branch for review. The host derives its "
        "repository, worktree, branch, and target from the active feature manifest, then opens "
        "or updates a pull request. This never merges or deploys."
    ),
    {
        "type": "object",
        "properties": {
            "feature_slug": {
                "type": "string",
                "minLength": 1,
                "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
            }
        },
        "required": ["feature_slug"],
        "additionalProperties": False,
    },
)
async def _publish_managed_feature_handle(
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    return await _request_publication(
        "publish_managed_feature",
        {
            "feature_slug": arguments["feature_slug"],
            "publication": "pull-request",
        },
    )


@tool(
    "rebase_managed_feature",
    (
        "Rebase one committed managed new-feature branch onto its verified remote default "
        "branch. The host derives its repository, worktree, branch, and target from the "
        "active feature manifest. This never pushes, opens a pull request, merges, or deploys."
    ),
    {
        "type": "object",
        "properties": {
            "feature_slug": {
                "type": "string",
                "minLength": 1,
                "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
            }
        },
        "required": ["feature_slug"],
        "additionalProperties": False,
    },
)
async def _rebase_managed_feature_handle(
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    return await _request_publication(
        "rebase_managed_feature",
        {"feature_slug": arguments["feature_slug"]},
    )


def _exit_container() -> NoReturn:
    """Write the close sentinel and terminate after an explicit context reset."""
    close_sentinel = _ipc.get_agent_tool_runtime().ipc_dir / "input" / "_close"
    close_sentinel.parent.mkdir(parents=True, exist_ok=True)
    close_sentinel.write_text("", encoding="utf-8")
    os._exit(0)


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
async def _reset_context_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    data: dict[str, str] = {
        "chatJid": _ipc.get_agent_tool_runtime().chat_jid,
        "groupFolder": _ipc.get_agent_tool_runtime().group_folder,
    }
    if arguments.get("message"):
        data["message"] = arguments["message"]
    _ipc.write_request_file("reset_context", data, reply_to=None)
    _exit_container()
