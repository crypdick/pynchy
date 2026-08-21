"""Config-backed automation tools for admin workspaces."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool

from . import _ipc
from ._ipc_request import ipc_service_request
from ._registry import ToolEntry, register, tool, tool_error

_AUTOMATION_FIELDS = {
    "workspace": {"type": "string", "minLength": 1},
    "prompt": {"type": "string", "minLength": 1},
    "command": {"type": "string", "minLength": 1},
    "cwd": {"type": "string", "minLength": 1},
    "schedule": {"type": "string", "minLength": 1},
    "interval_minutes": {"type": "integer", "minimum": 1},
    "at": {"type": "string", "minLength": 1},
    "display_name": {"type": "string", "minLength": 1},
    "agent": {"type": "boolean"},
    "reset_before_run": {"type": "boolean"},
    "memory": {"type": "boolean"},
}


def _admin_only() -> bool:
    return _ipc.get_agent_tool_runtime().is_admin


def _mutation_schema(*, require_definition: bool) -> dict[str, Any]:
    properties = {"name": {"type": "string", "minLength": 1}, **_AUTOMATION_FIELDS}
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["name"],
        "additionalProperties": False,
    }
    if require_definition:
        schema["required"] = ["name", "workspace"]
        schema["allOf"] = [
            {"anyOf": [{"required": ["prompt"]}, {"required": ["command"]}]},
            {
                "anyOf": [
                    {"required": ["schedule"]},
                    {"required": ["interval_minutes"]},
                    {"required": ["at"]},
                ]
            },
        ]
    return schema


async def _automation_status() -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    response = await ipc_service_request("automation_status", {}, response_timeout_seconds=10)
    if not response or response[0].text.startswith("Error:"):
        return tool_error(response[0].text if response else "Error: empty host response")
    try:
        result = json.loads(response[0].text)
    except json.JSONDecodeError:
        return tool_error("Error: host returned invalid automation data")
    if not isinstance(result.get("automations"), list):
        return tool_error("Error: host returned invalid automation data")
    return [TextContent(type="text", text=json.dumps(result))], result


def _list_automations_definition() -> Tool:
    return Tool(
        name="list_automations",
        description=(
            "List visible config-backed automations. Call this before creating or modifying "
            "an automation to find reusable definitions."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        outputSchema={
            "type": "object",
            "properties": {"automations": {"type": "array"}},
            "required": ["automations"],
            "additionalProperties": False,
        },
    )


async def _list_automations_handle(
    _arguments: dict[str, Any],
) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    return await _automation_status()


def _automation_definition_tool(name: str, description: str, schema: dict[str, Any]) -> Tool:
    return Tool(name=name, description=description, inputSchema=schema)


def _name_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1}},
        "required": ["name"],
        "additionalProperties": False,
    }


async def _request_definition(
    kind: str, arguments: dict[str, Any]
) -> list[TextContent] | CallToolResult:
    response = await ipc_service_request(kind, arguments, response_timeout_seconds=10)
    if not response or response[0].text.startswith("Error:"):
        return tool_error(response[0].text if response else "Error: empty host response")
    return response


def _get_automation_definition() -> Tool:
    return _automation_definition_tool(
        "get_automation",
        "Read one visible config-backed automation, including its prompt or command.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )


async def _get_automation_handle(arguments: dict[str, Any]) -> list[TextContent] | CallToolResult:
    return await _request_definition("automation_definition", arguments)


def _write_automation_request(
    kind: str, arguments: dict[str, Any]
) -> list[TextContent] | CallToolResult:
    if not _admin_only():
        return tool_error("Only the admin workspace can change automations.")
    _ipc.write_request_file(kind, arguments, reply_to=None)
    return [TextContent(type="text", text="Automation change requested; awaiting approval.")]


@tool(
    "create_automation",
    "Create a config-backed automation. Workspace commands require agent=false."
    " Inspect reusable definitions first.",
    _mutation_schema(require_definition=True),
    visible=_admin_only,
)
async def _create_automation_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    return _write_automation_request("create_automation", arguments)


@tool(
    "update_automation",
    "Update one config-backed automation. Changes are written to its automation definition.",
    _mutation_schema(require_definition=False),
    visible=_admin_only,
)
async def _update_automation_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    if len(arguments) == 1:
        return tool_error("Provide at least one automation field to update.")
    return _write_automation_request("update_automation", arguments)


@tool(
    "pause_automation",
    "Pause one config-backed automation without deleting its definition.",
    _name_schema(),
    visible=_admin_only,
)
async def _pause_automation_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    return _write_automation_request("pause_automation", arguments)


@tool(
    "resume_automation",
    "Resume one paused config-backed automation.",
    _name_schema(),
    visible=_admin_only,
)
async def _resume_automation_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    return _write_automation_request("resume_automation", arguments)


@tool(
    "delete_automation",
    "Delete one config-backed automation and its automation-owned files.",
    _name_schema(),
    visible=_admin_only,
)
async def _delete_automation_handle(  # noqa: RUF029 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    return _write_automation_request("delete_automation", arguments)


register(
    "list_automations",
    ToolEntry(definition=_list_automations_definition, handler=_list_automations_handle),
)
register(
    "get_automation",
    ToolEntry(definition=_get_automation_definition, handler=_get_automation_handle),
)
