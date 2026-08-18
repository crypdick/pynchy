"""Typed wire values exchanged around one agent execution."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves agent-runtime annotations at runtime.
)
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class AgentExecutionRuntime:
    """Concrete values resolved once for agent execution."""

    project_root: Path
    groups_dir: Path
    data_dir: Path
    mount_allowlist_path: Path
    blocked_mount_patterns: tuple[str, ...]
    agent_image: str
    agent_memory_mb: int
    container_timeout: float
    default_core: str
    idle_timeout: float
    model: str | None
    model_reasoning_effort: str | None


@dataclass(frozen=True)
class McpStartupFailure:
    """One MCP startup failure returned across the agent-execution boundary."""

    instance_id: str
    server_name: str
    reason: str


class InFlightWorkKind(StrEnum):
    """Agent work that can be resumed semantically after process loss."""

    INTERACTIVE = "interactive"
    RESET_HANDOFF = "reset_handoff"
    SCHEDULED = "scheduled"


class CheckpointControlState(StrEnum):
    """Durable human control over one unfinished agent checkpoint."""

    ACTIVE = "active"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESET_REQUESTED = "reset_requested"


@dataclass(frozen=True)
class InFlightTurn:
    """Durable checkpoint for one agent invocation that has not finalized."""

    turn_id: str
    chat_jid: str
    group_folder: str
    work_kind: InFlightWorkKind
    input_messages: list[dict[str, Any]]
    input_start_cursor: str
    input_end_cursor: str
    started_at: str
    task_id: str | None = None
    session_id: str | None = None
    output_sent: bool = False
    interrupted_at: str | None = None
    deploy_id: str | None = None
    claimed_at: str | None = None
    conversation_claim_id: str | None = None
    input_source: str = "user"
    control_state: CheckpointControlState = CheckpointControlState.ACTIVE


@dataclass
class ContainerInput:
    messages: list[dict[str, Any]]
    group_folder: str
    chat_jid: str
    is_admin: bool
    turn_id: str | None = None
    query_id: str | None = None
    session_id: str | None = None
    is_scheduled_task: bool = False
    automation_memory_dir: str | None = None
    input_source: str = "user"
    corruption_tainted: bool = False
    secret_tainted: bool = False
    system_notices: list[str] | None = None
    repo_access: str | None = None
    repo_accesses: list[str] = field(default_factory=list)
    agent_core_module: str = "agent_runner.cores.openai"
    agent_core_class: str = "OpenAIAgentCore"
    agent_core_config: dict[str, Any] | None = None
    plugin_hooks: list[dict[str, str]] = field(default_factory=list)
    system_prompt_append: str | None = None
    invocation_ts: float = 0.0
    mcp_gateway_url: str | None = None
    mcp_gateway_key: str | None = None
    mcp_direct_servers: list[dict[str, Any]] | None = None
    agent_tool_grants: list[str] | None = None


@dataclass
class ContainerOutput:
    status: Literal["success", "error"]
    result: str | None = None
    new_session_id: str | None = None
    error: str | None = None
    type: str = "result"
    thinking: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    text: str | None = None
    system_subtype: str | None = None
    system_data: dict[str, Any] | None = None
    tool_result_id: str | None = None
    tool_result_content: str | None = None
    tool_result_is_error: bool | None = None
    result_metadata: dict[str, Any] | None = None
    query_id: str | None = None


def input_to_dict(input_data: ContainerInput) -> dict[str, Any]:
    """Convert container input to its compact JSON-ready wire representation."""
    return {
        item.name: value
        for item in fields(input_data)
        if (value := getattr(input_data, item.name)) is not None and value != []
    }


def parse_container_output(json_str: str) -> ContainerOutput:
    """Parse agent-runner JSON while ignoring unrecognized extra fields."""
    data = json.loads(json_str)
    known = {item.name for item in fields(ContainerOutput)}
    return ContainerOutput(**{key: value for key, value in data.items() if key in known})


OnOutput = Callable[[ContainerOutput], Awaitable[None]]


@dataclass
class VolumeMount:
    host_path: str
    container_path: str
    readonly: bool = False
