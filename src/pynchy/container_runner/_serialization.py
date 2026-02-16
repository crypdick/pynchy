"""Serialization helpers — camelCase/snake_case boundary crossing.

Converts ContainerInput to dict for JSON transport into the container,
and parses JSON output from the container back to ContainerOutput.
"""

from __future__ import annotations

import json
from typing import Any

from pynchy.types import ContainerInput, ContainerOutput


def _input_to_dict(input_data: ContainerInput) -> dict[str, Any]:
    """Convert ContainerInput to dict for the Python agent-runner."""
    d: dict[str, Any] = {
        "messages": input_data.messages,
        "group_folder": input_data.group_folder,
        "chat_jid": input_data.chat_jid,
        "is_god": input_data.is_god,
    }
    if input_data.session_id is not None:
        d["session_id"] = input_data.session_id
    if input_data.is_scheduled_task:
        d["is_scheduled_task"] = True
    if input_data.plugin_mcp_servers is not None:
        d["plugin_mcp_servers"] = input_data.plugin_mcp_servers
    if input_data.system_notices:
        d["system_notices"] = input_data.system_notices
    if input_data.project_access:
        d["project_access"] = True
    # Always include agent core fields (container needs them to import the core)
    d["agent_core_module"] = input_data.agent_core_module
    d["agent_core_class"] = input_data.agent_core_class
    if input_data.agent_core_config is not None:
        d["agent_core_config"] = input_data.agent_core_config
    return d


def _parse_container_output(json_str: str) -> ContainerOutput:
    """Parse JSON from the Python agent-runner into ContainerOutput."""
    data = json.loads(json_str)
    return ContainerOutput(
        status=data["status"],
        result=data.get("result"),
        new_session_id=data.get("new_session_id"),
        error=data.get("error"),
        type=data.get("type", "result"),
        thinking=data.get("thinking"),
        tool_name=data.get("tool_name"),
        tool_input=data.get("tool_input"),
        text=data.get("text"),
        system_subtype=data.get("system_subtype"),
        system_data=data.get("system_data"),
        tool_result_id=data.get("tool_result_id"),
        tool_result_content=data.get("tool_result_content"),
        tool_result_is_error=data.get("tool_result_is_error"),
        result_metadata=data.get("result_metadata"),
    )
