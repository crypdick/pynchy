"""Route-scoped Matrix connection tools and plugin contributions."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pluggy
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pynchy.actions.api import ActionId
from pynchy.conversation.api import (
    ConversationId,
)
from pynchy.identifiers import (
    ChatJid,
)
from pynchy.plugins.api import (
    ActionIntentContract,
    ActionIntentDraft,
    ActionIntentReceipt,
    ApprovalContract,
    ApprovalTrigger,
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
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    ProbeStatus,
)
from pynchy.plugins.integrations.matrix_connection import (
    MatrixConnectionOperations,
    MatrixConnectionRuntime,
    _validate_portal,
)
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixGatewayError,
    MatrixRouteGateway,
    MatrixSendResult,
    create_matrix_gateway_client,
    json_result,
    matrix_connection_state_dir,
)
from pynchy.plugins.integrations.matrix_route_registry import (
    ActiveMatrixRoute,
    get_active_matrix_route,
)
from pynchy.plugins.integrations.matrix_route_resolution import (
    ResolvedMatrixRoute,
)

hookimpl = pluggy.HookimplMarker("pynchy")
_MAX_LIST_LIMIT = 250


@dataclass(frozen=True)
class MatrixConnectionRuntimeOptions:
    """Connection values needed to construct one Matrix poller."""

    name: str
    poll_interval_seconds: float


@dataclass(frozen=True)
class MatrixGatewayRuntime:
    """Resolved Matrix routing and state-root configuration."""

    data_dir: Path
    routes: tuple[ResolvedMatrixRoute, ...]
    connections: tuple[MatrixConnectionRuntimeOptions, ...]
    get_control_thread_jid: Callable[[ConversationId], Awaitable[ChatJid | None]]
    connection_operations: MatrixConnectionOperations


_runtime: MatrixGatewayRuntime | None = None


def configure_matrix_gateway_runtime(runtime: MatrixGatewayRuntime) -> None:
    """Set Matrix connection configuration before plugin runtime loading."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> MatrixGatewayRuntime:
    if _runtime is None:
        raise RuntimeError("Matrix gateway runtime has not been configured")
    return _runtime


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RouteReadArguments(_StrictModel):
    limit: int = Field(default=50, ge=1, le=_MAX_LIST_LIMIT)


class _RouteSendArguments(_StrictModel):
    body: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be empty")
        return value


class _RouteSendReceipt(_StrictModel):
    """Agent-safe provider receipt without destination identifiers."""

    event_id: str = Field(min_length=1)


def _active_route(data: dict[str, Any]) -> ActiveMatrixRoute:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        raise ValueError("Matrix route tools require an active conversation workspace")
    active = get_active_matrix_route(source_group)
    if active is None:
        raise ValueError("This workspace is not bound to a configured Matrix route")
    return active


def _gateway_for(active: ActiveMatrixRoute) -> MatrixRouteGateway:
    command = os.environ.get(active.route.connection.gateway_command_env)
    state_dir = matrix_connection_state_dir(
        _configured_runtime().data_dir, active.route.connection_name
    )
    return create_matrix_gateway_client(command, state_dir=state_dir)


def _read_arguments(data: dict[str, Any]) -> _RouteReadArguments:
    return _RouteReadArguments.model_validate({"limit": data.get("limit", 50)})


def _send_arguments(data: dict[str, Any]) -> _RouteSendArguments:
    return _RouteSendArguments.model_validate({"body": data.get("body")})


def _send_message_draft(data: dict[str, Any]) -> ActionIntentDraft:
    """Bind approval to the current route, room, portal, thread, and body."""
    active = _active_route(data)
    if active.route.outbound == "read_only":
        raise ValueError("Matrix route is read-only")
    arguments = _send_arguments(data)
    return ActionIntentDraft(
        recipient=f"matrix-route:{active.route.name}",
        payload={
            "connection": active.route.connection_name,
            "route": active.route.name,
            "conversation_id": active.conversation_id,
            "approval_chat_jid": active.control_thread_jid,
            "room_id": active.route.endpoint.room_id,
            "portal": active.portal.model_dump(mode="json"),
            "body": arguments.body,
        },
        summary=f"Send a Matrix reply on route {active.route.name}",
    )


def _send_message_receipt(response: dict[str, Any]) -> ActionIntentReceipt:
    raw_result = response.get("result")
    if not isinstance(raw_result, str):
        raise TypeError("Matrix send response omitted its serialized provider result")
    result = _RouteSendReceipt.model_validate(json.loads(raw_result))
    return ActionIntentReceipt(
        provider_request_id=result.event_id,
        receipt=result.model_dump(mode="json"),
    )


async def _current_active_route(data: dict[str, Any]) -> ActiveMatrixRoute:
    active = _active_route(data)
    thread_jid = await _configured_runtime().get_control_thread_jid(active.conversation_id)
    if thread_jid != active.control_thread_jid:
        raise ValueError("Matrix conversation control binding changed")
    return active


async def _handle_route_read(data: dict[str, Any]) -> dict[str, Any]:
    """Read history from only the room bound to the active conversation."""
    try:
        active = await _current_active_route(data)
        arguments = _read_arguments(data)
        client = _gateway_for(active)
        await _revalidate_portal(active, client)
        messages = await asyncio.to_thread(
            client.list_messages,
            room_id=active.route.endpoint.room_id,
            limit=arguments.limit,
        )
    except (MatrixGatewayError, ValidationError, ValueError) as exc:
        return {"error": f"Matrix route read denied: {exc}"}
    scoped = [
        {
            "event_id": message.event_id,
            "sender": message.sender,
            "origin_server_ts": message.origin_server_ts,
            "body": message.body,
        }
        for message in messages
    ]
    return {"result": json.dumps(scoped, indent=2, sort_keys=True)}


async def _revalidate_portal(
    active: ActiveMatrixRoute,
    client: MatrixRouteGateway,
) -> None:
    """Prove the live room and portal still match the registered route."""
    assertion = await asyncio.to_thread(
        client.room_assertion,
        room_id=active.route.endpoint.room_id,
    )
    _validate_portal(active.route, assertion)
    if assertion != active.portal:
        raise MatrixGatewayError("Matrix route portal changed; refresh the conversation route")


async def _send_route_message(active: ActiveMatrixRoute, body: str) -> MatrixSendResult:
    client = _gateway_for(active)
    await _revalidate_portal(active, client)
    result = await asyncio.to_thread(
        client.send_message,
        room_id=active.route.endpoint.room_id,
        body=body,
    )
    if result.room_id != active.route.endpoint.room_id:
        raise MatrixGatewayError("Matrix gateway returned an unexpected destination")
    return result


async def _handle_route_send(data: dict[str, Any]) -> dict[str, Any]:
    """Send only after rechecking the exact configured room and portal."""
    try:
        active = await _current_active_route(data)
        if active.route.outbound == "read_only":
            return {"error": "Matrix route is read-only"}
        arguments = _send_arguments(data)
        result = await _send_route_message(active, arguments.body)
    except (MatrixGatewayError, ValidationError, ValueError) as exc:
        return {"error": f"Matrix route send denied: {exc}"}
    return {"result": json_result(_RouteSendReceipt(event_id=result.event_id))}


def _gateway_executable_exists(command: str) -> bool:
    path = Path(command).expanduser()
    if path.parent != Path():
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


async def _probe_matrix_gateway(context: CapabilityProbeContext) -> CapabilityProbeResult:
    active = get_active_matrix_route(context.workspace)
    if active is None:
        return CapabilityProbeResult(ProbeStatus.DEGRADED, "No active Matrix route binding")
    command = os.environ.get(active.route.connection.gateway_command_env, "pynchy-matrix-gateway")
    if await asyncio.to_thread(_gateway_executable_exists, command):
        return CapabilityProbeResult(ProbeStatus.READY)
    return CapabilityProbeResult(ProbeStatus.UNAVAILABLE, "Matrix gateway binary is unavailable")


def _matrix_action(
    action_id: str,
    tool_name: str,
    summary: str,
    handler: HostActionHandler,
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
                    description=f"Enable {tool_name} in the routed workspace profile.",
                ),
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.CONFIG,
                    name="matrix-route",
                    description="Configure a named Matrix connection, endpoint, and exact route.",
                ),
            ),
            documentation="docs/integrations/matrix-gateway.md",
            probe=_probe_matrix_gateway,
        ),
        tool_name=HostToolName(tool_name),
        handler=handler,
        access=access,
        approval=ApprovalContract(
            trigger=(
                ApprovalTrigger.ALWAYS
                if access is HostActionAccess.WRITE
                else ApprovalTrigger.SERVICE_POLICY
            )
        ),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
        action_intent=(
            ActionIntentContract(
                provider="matrix-gateway",
                draft_from_request=_send_message_draft,
                receipt_from_response=_send_message_receipt,
            )
            if access is HostActionAccess.WRITE
            else None
        ),
    )


MATRIX_HOST_ACTIONS = HostActionRegistration(
    actions=(
        _matrix_action(
            "chat.matrix.route.read",
            "matrix_route_read",
            "Read messages from the Matrix route bound to this conversation.",
            _handle_route_read,
            access=HostActionAccess.READ,
        ),
        _matrix_action(
            "chat.matrix.route.send",
            "matrix_route_send",
            "Send an exactly approved reply on this conversation's Matrix route.",
            _handle_route_send,
            access=HostActionAccess.WRITE,
        ),
    )
)


class MatrixGatewayPlugin:  # noqa: V102
    """Provide Matrix connection runtimes and route-scoped host actions."""

    @hookimpl
    def pynchy_connection_runtime(self) -> tuple[MatrixConnectionRuntime, ...]:
        runtime = _configured_runtime()
        return tuple(
            MatrixConnectionRuntime(
                connection_name,
                tuple(
                    route for route in runtime.routes if route.connection_name == connection_name
                ),
                poll_interval_seconds=connection.poll_interval_seconds,
                state_dir=matrix_connection_state_dir(runtime.data_dir, connection_name),
                operations=runtime.connection_operations,
            )
            for connection in runtime.connections
            if any(route.connection_name == connection.name for route in runtime.routes)
            for connection_name in (connection.name,)
        )

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return MATRIX_HOST_ACTIONS
