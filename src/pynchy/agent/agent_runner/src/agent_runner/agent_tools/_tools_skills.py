"""Discover and request access to vault-backed Pynchy skills."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent

from ._ipc_request import ipc_service_request
from ._registry import tool, tool_error

_SKILL_ROOT_ENV = "PYNCHY_SKILLS_ROOT"
_MAX_SEARCH_RESULTS = 12
_MAX_SKILL_CONTENT_CHARS = 40_000


def _skill_dirs() -> dict[str, Path]:
    """Return readable skill directories keyed by their public directory name."""
    result: dict[str, Path] = {}
    for env_name in (_SKILL_ROOT_ENV,):
        root_value = os.environ.get(env_name)
        if not root_value:
            continue
        root = Path(root_value)
        try:
            candidates = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for candidate in candidates:
            skill_md = candidate / "SKILL.md"
            if (
                candidate.is_dir()
                and skill_md.is_file()
                and not candidate.is_symlink()
                and not skill_md.is_symlink()
            ):
                result[candidate.name] = candidate
    return result


def _skill_summary(skill_name: str, skill_dir: Path) -> str:
    try:
        lines = (skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return skill_name

    description = ""
    for line in lines[:80]:
        if line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip('"')
            break
    if description:
        return f"{skill_name}: {description}"
    for line in lines:
        if line.startswith("# "):
            return f"{skill_name}: {line[2:].strip()}"
    return skill_name


def _matching_skills(query: str) -> list[tuple[str, str]]:
    query_terms = [part for part in query.lower().split() if part]
    matches: list[tuple[str, str]] = []
    for name, skill_dir in _skill_dirs().items():
        summary = _skill_summary(name, skill_dir)
        searchable = summary.lower()
        if all(term in searchable for term in query_terms):
            matches.append((name, summary))
    return matches[:_MAX_SEARCH_RESULTS]


def _response_payload(response: list[TextContent]) -> dict[str, object] | None:
    if not response:
        return None
    try:
        payload = json.loads(response[0].text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _skill_content(skill_name: str) -> str | None:
    skill_dir = _skill_dirs().get(skill_name)
    if skill_dir is None:
        return None
    try:
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    if len(content) <= _MAX_SKILL_CONTENT_CHARS:
        return content
    return content[:_MAX_SKILL_CONTENT_CHARS] + "\n\n[Skill truncated at 40,000 characters.]"


def _access_granted(skill_name: str, *, persistent: bool) -> list[TextContent]:
    content = _skill_content(skill_name)
    if content is None:
        return [TextContent(type="text", text=f"Skill '{skill_name}' is no longer available.")]
    scope = "future turns through this profile" if persistent else "this conversation only"
    return [
        TextContent(
            type="text",
            text=(
                f"Access granted to '{skill_name}' for {scope}.\n\n"
                f'<skill name="{skill_name}">\n{content}\n</skill>'
            ),
        )
    ]


@tool(
    "search_skills",
    "Search the vault-backed Pynchy skill catalog. Use this before guessing at a workflow.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words describing the task or capability to find.",
            }
        },
        "required": ["query"],
    },
)
async def _search_skills_handle(  # noqa: RUF029, RUF100 - async MCP handler contract.
    arguments: dict[str, Any],
) -> list[TextContent]:
    query = str(arguments.get("query", "")).strip()
    if not query:
        return [TextContent(type="text", text="Provide a non-empty skill search query.")]
    if not _skill_dirs():
        return [
            TextContent(
                type="text",
                text="The Pynchy skill catalog is unavailable in this session.",
            )
        ]
    matches = _matching_skills(query)
    if not matches:
        return [TextContent(type="text", text=f"No Pynchy skills matched '{query}'.")]
    lines = [f"- {name}: {summary.removeprefix(f'{name}: ')}" for name, summary in matches]
    return [
        TextContent(
            type="text",
            text=(
                "Matching Pynchy skills:\n"
                + "\n".join(lines)
                + "\n\nUse request_skill_access with an exact skill name to activate one."
            ),
        )
    ]


async def _current_access_result(skill_name: str) -> list[TextContent] | CallToolResult | None:
    """Return an existing policy result, or None when user input is needed."""
    status_response = await ipc_service_request(
        "skill_access",
        {"action": "status", "skill_name": skill_name},
        type_override="skill_access:policy",
    )
    status_payload = _response_payload(status_response)
    status = status_payload.get("status") if status_payload else None
    if status == "granted":
        return _access_granted(skill_name, persistent=True)
    if status == "denied":
        return tool_error(f"Access to '{skill_name}' is denied by this workspace profile.")
    if status not in {"available", None}:
        return tool_error(f"Unable to check access for '{skill_name}'.")
    return None


async def _ask_for_access_choice(skill_name: str, reason: str) -> tuple[str | None, str | None]:
    """Ask the workspace owner for a one-time or persistent access choice."""
    answer_response = await ipc_service_request(
        "ask_user",
        {
            "questions": [
                {
                    "question": f"Allow Pynchy to use '{skill_name}'? {reason}",
                    "skill_access": {"skill_name": skill_name},
                    "options": [
                        {
                            "label": "Grant once",
                            "description": "Use it for this conversation only.",
                        },
                        {
                            "label": "Grant always",
                            "description": "Add it to this workspace profile.",
                        },
                        {
                            "label": "Deny once",
                            "description": "Do not use it now; ask again later if needed.",
                        },
                        {
                            "label": "Deny always",
                            "description": "Block it for this workspace profile.",
                        },
                    ],
                }
            ]
        },
        type_override="ask_user:ask",
    )
    answer_payload = _response_payload(answer_response) or {}
    answers = answer_payload.get("answers")
    choice = answers.get("answer") if isinstance(answers, dict) else None
    status = answer_payload.get("skill_access_status")
    return (
        choice.strip().lower() if isinstance(choice, str) else None,
        status if isinstance(status, str) else None,
    )


def _one_time_access_result(skill_name: str, choice: str) -> list[TextContent] | None:
    """Return the result for a one-time choice, if *choice* is one."""
    if choice == "grant once":
        return _access_granted(skill_name, persistent=False)
    if choice == "deny once":
        return [TextContent(type="text", text=f"Access to '{skill_name}' was denied for now.")]
    return None


def _apply_access_choice(
    skill_name: str, choice: str | None, persistent_status: str | None
) -> list[TextContent] | CallToolResult:
    """Apply a one-time choice or persist an always-choice through host IPC."""
    if choice is None:
        return tool_error("The skill-access question did not return a valid choice.")
    if one_time_result := _one_time_access_result(skill_name, choice):
        return one_time_result
    if choice not in {"grant always", "deny always"}:
        return tool_error(f"Unrecognized skill-access choice: {choice}")
    if persistent_status == "granted":
        return _access_granted(skill_name, persistent=True)
    if persistent_status == "denied":
        return [TextContent(type="text", text=f"Access to '{skill_name}' was denied permanently.")]
    return tool_error(f"Unable to persist access policy for '{skill_name}'.")


@tool(
    "request_skill_access",
    "Request access to a named Pynchy skill. The user can grant or deny it once or always.",
    {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Exact skill name returned by search_skills.",
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of why the skill would help.",
            },
        },
        "required": ["skill_name", "reason"],
    },
)
async def _request_skill_access_handle(
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    skill_name = str(arguments.get("skill_name", "")).strip()
    reason = str(arguments.get("reason", "")).strip()
    if not skill_name or not reason:
        return tool_error("skill_name and reason are required")
    if skill_name not in _skill_dirs():
        return tool_error(f"Unknown Pynchy skill: '{skill_name}'. Search the catalog first.")

    if current_result := await _current_access_result(skill_name):
        return current_result
    choice, persistent_status = await _ask_for_access_choice(skill_name, reason)
    return _apply_access_choice(skill_name, choice, persistent_status)
