"""Host-side Matrix communications service handlers.

The agent sees these through Pynchy's built-in stdio MCP server, rather than a
second remote MCP server.  That keeps the Matrix session on the host and lets
the normal IPC approval boundary intercept every external send.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pluggy
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pynchy.actions import ActionId
from pynchy.capabilities import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityProbeContext,
    CapabilityProbeResult,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    ProbeStatus,
)
from pynchy.config import get_settings
from pynchy.plugins.integrations.matrix_gateway_client import (
    DEFAULT_GATEWAY_COMMAND,
    MatrixGatewayError,
    create_matrix_gateway_client,
    json_result,
)

hookimpl = pluggy.HookimplMarker("pynchy")

_MAX_LIST_LIMIT = 250
type MatrixHandler = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ListMessagesArguments(_StrictModel):
    """Arguments for Matrix message listing."""

    room_id: str = Field(min_length=1)
    limit: int = Field(default=50, ge=1, le=_MAX_LIST_LIMIT)

    @field_validator("room_id")
    @classmethod
    def _validate_room_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("room_id must be a single non-empty line")
        return normalized


class _SendMessageArguments(_StrictModel):
    """Arguments for one approval-gated external Matrix message."""

    room_id: str = Field(min_length=1)
    body: str = Field(min_length=1)

    @field_validator("room_id")
    @classmethod
    def _validate_room_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("room_id must be a single non-empty line")
        return normalized

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be empty")
        return value


def _message_arguments(data: dict[str, Any]) -> _ListMessagesArguments:
    return _ListMessagesArguments.model_validate(
        {"room_id": data.get("room_id"), "limit": data.get("limit", 50)}
    )


def _send_arguments(data: dict[str, Any]) -> _SendMessageArguments:
    return _SendMessageArguments.model_validate(
        {"room_id": data.get("room_id"), "body": data.get("body")}
    )


def _workspace_enables_tool(data: dict[str, Any], tool_name: str) -> bool:
    """Keep private Matrix access scoped to an explicitly configured workspace."""
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return False
    try:
        resolved = get_settings().resolved_workspace_config(source_group)
    except ValueError:
        return False
    return resolved is not None and tool_name in resolved.tools


def _only_in_enabled_workspace(tool_name: str, handler: MatrixHandler) -> MatrixHandler:
    """Deny Matrix access outside a workspace that explicitly selected the tool."""

    async def guarded(data: dict[str, Any]) -> dict[str, object]:
        if not _workspace_enables_tool(data, tool_name):
            return {"error": f"{tool_name} is not enabled for this workspace"}
        return await handler(data)

    return guarded


async def _handle_list_chats(_data: dict[str, Any]) -> dict[str, object]:
    """List Matrix rooms visible to the host-owned gateway session."""
    try:
        result = await asyncio.to_thread(create_matrix_gateway_client().list_chats)
    except MatrixGatewayError as exc:
        return {"error": str(exc)}
    return {"result": json_result(cast("list[BaseModel]", result))}


async def _handle_list_messages(data: dict[str, Any]) -> dict[str, object]:
    """Read recent messages from one Matrix room without changing it."""
    try:
        arguments = _message_arguments(data)
        result = await asyncio.to_thread(
            create_matrix_gateway_client().list_messages,
            room_id=arguments.room_id,
            limit=arguments.limit,
        )
    except (MatrixGatewayError, ValidationError) as exc:
        return {"error": f"Invalid Matrix gateway tool arguments: {exc}"}
    return {"result": json_result(cast("list[BaseModel]", result))}


async def _handle_send_message(data: dict[str, Any]) -> dict[str, object]:
    """Send a validated, approval-gated Matrix message as the gateway owner."""
    try:
        arguments = _send_arguments(data)
        result = await asyncio.to_thread(
            create_matrix_gateway_client().send_message,
            room_id=arguments.room_id,
            body=arguments.body,
        )
    except (MatrixGatewayError, ValidationError) as exc:
        return {"error": f"Invalid Matrix gateway tool arguments: {exc}"}
    return {"result": json_result(result)}


def _gateway_executable_exists(command: str) -> bool:
    path = Path(command).expanduser()
    if path.parent != Path():
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


async def _probe_matrix_gateway(_context: CapabilityProbeContext) -> CapabilityProbeResult:
    """Check only local executable readiness; never contact Matrix from status."""
    command = os.environ.get("PYNCHY_MATRIX_GATEWAY", DEFAULT_GATEWAY_COMMAND)
    available = await asyncio.to_thread(_gateway_executable_exists, command)
    if available:
        return CapabilityProbeResult(ProbeStatus.READY)
    return CapabilityProbeResult(
        ProbeStatus.UNAVAILABLE,
        "Matrix gateway binary is unavailable; configure PYNCHY_MATRIX_GATEWAY",
    )


def _matrix_action(
    action_id: str,
    tool_name: str,
    summary: str,
    handler: MatrixHandler,
    *,
    access: HostActionAccess,
) -> HostActionDescriptor:
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="matrix-gateway",
            summary=summary,
            action_ids=(ActionId(action_id),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name=tool_name,
                    description=f"Enable the {tool_name} tool for this workspace.",
                ),
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.HOST_BINARY,
                    name="PYNCHY_MATRIX_GATEWAY",
                    description="Install the Matrix gateway or configure its executable path.",
                ),
            ),
            setup_hint="Follow the Matrix gateway setup guide and enable this tool in a profile.",
            recovery_hint="Verify the gateway executable and Matrix device session.",
            documentation="docs/usage/matrix-gateway.md",
            probe=_probe_matrix_gateway,
        ),
        tool_name=HostToolName(tool_name),
        handler=_only_in_enabled_workspace(tool_name, handler),
        access=access,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
    )


MATRIX_HOST_ACTIONS = HostActionRegistration(
    actions=(
        _matrix_action(
            "chat.matrix.list",
            "matrix_list_chats",
            "List chats through the host-only Matrix communications gateway.",
            _handle_list_chats,
            access=HostActionAccess.READ,
        ),
        _matrix_action(
            "chat.matrix.message.list",
            "matrix_list_messages",
            "Read recent messages from one Matrix chat.",
            _handle_list_messages,
            access=HostActionAccess.READ,
        ),
        _matrix_action(
            "chat.matrix.message.send",
            "matrix_send_message",
            "Send an approved message as the Matrix gateway owner.",
            _handle_send_message,
            access=HostActionAccess.WRITE,
        ),
    )
)


class MatrixGatewayPlugin:
    """Expose host-only Matrix operations through Pynchy's IPC service boundary."""

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return MATRIX_HOST_ACTIONS
