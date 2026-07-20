"""IPC handler for approval decision files.

When a decision file appears in approval_decisions/, this handler:
- Reads the decision and corresponding pending approval
- Executes the pending request (if approved) or writes error (if denied)
- Writes the IPC response file so the container unblocks
- Cleans up pending and decision files

Approved service actions re-check current policy before execution; the human
decision satisfies only the approval requirement, not later policy
denial or descriptor removal.

Two handler types are supported:
- "service" (default): dispatches through plugin handlers (MCP service requests)
- "ipc": dispatches through ipc._registry.dispatch() with _cop_approved=True
  (host-mutating operations that went through cop_gate)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves approval decision paths at runtime.
)
from typing import TYPE_CHECKING, Any, cast

from pynchy.capabilities import (  # noqa: TC001, RUF100 - beartype resolves runtime annotations.
    ApprovalMode,
    HostActionDescriptor,
)
from pynchy.config import Settings, get_settings
from pynchy.conversation.models import ConversationId
from pynchy.host.container_manager.action_intents import execute_action_intent
from pynchy.host.container_manager.ipc import registry
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext as _ApprovalDecisionContext,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    approval_replay_gate as _approval_replay_gate,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    approval_replay_validation_error as _approval_replay_validation_error,
)
from pynchy.host.container_manager.ipc.handlers_service import _get_action_catalog
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import approval as security_approval
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    evaluate_host_action_policy,
)
from pynchy.logger import logger
from pynchy.state import (
    approve_action_intent,
    deny_action_intent,
    fail_action_intent,
)

if TYPE_CHECKING:
    from pynchy.host.container_manager.ipc.deps import IpcDeps


@dataclass(frozen=True)
class _ApprovedServiceContext:
    request_data: dict[str, Any]
    source_group: str
    request_id: str
    tool_name: str
    chat_jid: str
    action: HostActionDescriptor | None
    gate: SecurityGate


def _read_json_file(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def _path_exists(path: Path) -> bool:
    return path.exists()


def _unlink_missing_ok(path: Path) -> None:
    path.unlink(missing_ok=True)


def _unlink_all_missing_ok(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


async def process_approval_decision(
    decision_file: Path, source_group: str, *, deps: object | None = None
) -> None:
    """Process an approval decision file — execute or deny the pending request."""
    try:
        decision = await asyncio.to_thread(_read_json_file, decision_file)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read decision file", path=str(decision_file), err=str(exc))
        await asyncio.to_thread(_unlink_missing_ok, decision_file)
        return

    request_id = decision.get("request_id")
    if not request_id:
        logger.warning("Decision file missing request_id", path=str(decision_file))
        await asyncio.to_thread(_unlink_missing_ok, decision_file)
        return

    # Find the corresponding pending approval
    s = get_settings()
    pending_file = s.data_dir / "ipc" / source_group / "pending_approvals" / f"{request_id}.json"

    if not await asyncio.to_thread(_path_exists, pending_file):
        logger.warning("No pending approval for decision", request_id=request_id)
        await asyncio.to_thread(_unlink_missing_ok, decision_file)
        return

    try:
        pending = await asyncio.to_thread(_read_json_file, pending_file)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read pending file", path=str(pending_file), err=str(exc))
        await asyncio.to_thread(_unlink_all_missing_ok, decision_file, pending_file)
        return

    context = _approval_decision_context(
        pending,
        decision,
        request_id=request_id,
        source_group=source_group,
        settings=s,
    )
    await _dispatch_approval_decision(context, deps)
    await asyncio.to_thread(_unlink_all_missing_ok, pending_file, decision_file)


def _approval_decision_context(
    pending: dict[str, Any],
    decision: dict[str, Any],
    *,
    request_id: str,
    source_group: str,
    settings: Settings,
) -> _ApprovalDecisionContext:
    tool_name = pending.get("tool_name", "unknown")
    chat_jid = pending.get("approval_chat_jid", "unknown")
    request_data = pending.get("request_data", {})
    handler_type = pending.get("handler_type", "service")
    action = _get_action_catalog().action_for(tool_name) if handler_type == "service" else None
    capability_id = str(action.capability.id) if action is not None else None
    action_ids = (
        tuple(str(action_id) for action_id in action.capability.action_ids)
        if action is not None
        else ()
    )
    approved = decision.get("approved", False)
    decided_by = decision.get("decided_by")
    approver = decided_by if isinstance(decided_by, str) and decided_by else "user"
    decided_at = decision.get("decided_at")
    approved_at = (
        decided_at if isinstance(decided_at, str) and decided_at else datetime.now(UTC).isoformat()
    )
    origin = pending.get("origin_conversation_id")
    origin_conversation_id = ConversationId(origin) if isinstance(origin, str) and origin else None
    gate = _approval_replay_gate(
        settings,
        source_group,
        require_resolved=origin_conversation_id is not None,
    )
    action_payload = pending.get("action_payload")
    raw_timeout = pending.get("expires_after_seconds", security_approval.APPROVAL_TIMEOUT_SECONDS)
    return _ApprovalDecisionContext(
        request_id=request_id,
        source_group=source_group,
        tool_name=tool_name,
        chat_jid=chat_jid,
        request_data=request_data,
        approved=approved,
        approver=approver,
        approved_at=approved_at,
        handler_type=handler_type,
        action=action,
        gate=gate,
        capability_id=capability_id,
        action_ids=action_ids,
        origin_conversation_id=origin_conversation_id,
        action_payload=action_payload if isinstance(action_payload, dict) else None,
        action_payload_sha256=(
            pending.get("action_payload_sha256")
            if isinstance(pending.get("action_payload_sha256"), str)
            else None
        ),
        requested_at=(
            pending.get("timestamp") if isinstance(pending.get("timestamp"), str) else None
        ),
        expires_after_seconds=(
            raw_timeout if isinstance(raw_timeout, int) and raw_timeout > 0 else 0
        ),
    )


async def _dispatch_approval_decision(
    context: _ApprovalDecisionContext, deps: object | None
) -> None:
    if context.handler_type == "mcp_proxy":
        await _resolve_mcp_proxy_approval(context)
        return
    if context.approved:
        await _dispatch_approved_request(context, deps)
    else:
        await _dispatch_denied_request(context)


async def _resolve_mcp_proxy_approval(context: _ApprovalDecisionContext) -> None:
    """Resolve an in-process proxy request without re-dispatching it."""
    resolved = security_approval.resolve_mcp_proxy_approval(
        context.request_id, approved=context.approved
    )
    if not resolved:
        logger.warning(
            "MCP proxy approval Future not found (timed out?)",
            request_id=context.request_id,
        )
    await record_security_event(
        chat_jid=context.chat_jid,
        workspace=context.source_group,
        tool_name=context.tool_name,
        decision="approved_by_user" if context.approved else "denied_by_user",
        request_id=context.request_id,
        capability_id=context.capability_id,
        action_ids=context.action_ids,
    )


async def _dispatch_approved_request(
    context: _ApprovalDecisionContext, deps: object | None
) -> None:
    validation_error = await _approval_replay_validation_error(context)
    if validation_error is not None:
        await fail_action_intent(context.request_id, reason=validation_error)
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(context.source_group, context.request_id),
            {"error": f"Approval is stale: {validation_error}"},
        )
        await record_security_event(
            chat_jid=context.chat_jid,
            workspace=context.source_group,
            tool_name=context.tool_name,
            decision="execution_failed",
            reason=validation_error,
            request_id=context.request_id,
            capability_id=context.capability_id,
            action_ids=context.action_ids,
        )
        return
    if context.handler_type == "ipc":
        await _execute_ipc_approval(
            context.request_data, context.source_group, context.request_id, deps
        )
    else:
        if context.gate is None:
            raise RuntimeError("Approval replay policy disappeared after validation")
        if context.action is not None and context.action.action_intent is not None:
            await approve_action_intent(
                context.request_id,
                approver=context.approver,
                approved_at=context.approved_at,
                policy_decision="approved by user",
            )
        await _execute_service_approval(
            _ApprovedServiceContext(
                request_data=context.request_data,
                source_group=context.source_group,
                request_id=context.request_id,
                tool_name=context.tool_name,
                chat_jid=context.chat_jid,
                action=context.action,
                gate=context.gate,
            )
        )
    await record_security_event(
        chat_jid=context.chat_jid,
        workspace=context.source_group,
        tool_name=context.tool_name,
        decision="approved_by_user",
        request_id=context.request_id,
        capability_id=context.capability_id,
        action_ids=context.action_ids,
    )


async def _dispatch_denied_request(context: _ApprovalDecisionContext) -> None:
    await deny_action_intent(context.request_id, reason="Denied by user")
    await asyncio.to_thread(
        write_ipc_response,
        ipc_response_path(context.source_group, context.request_id),
        {"error": "Denied by user"},
    )
    await record_security_event(
        chat_jid=context.chat_jid,
        workspace=context.source_group,
        tool_name=context.tool_name,
        decision="denied_by_user",
        request_id=context.request_id,
        capability_id=context.capability_id,
        action_ids=context.action_ids,
    )


async def _execute_service_approval(
    context: _ApprovedServiceContext,
) -> None:
    """Dispatch an approved service request through plugin handlers."""
    action = context.action
    if action is None:
        await fail_action_intent(
            context.request_id,
            reason="Host action descriptor is unavailable after approval.",
        )
        logger.warning("Approved tool no longer available", tool_name=context.tool_name)
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(context.source_group, context.request_id),
            {"error": f"Approved but tool '{context.tool_name}' is no longer available"},
        )
        await record_security_event(
            chat_jid=context.chat_jid,
            workspace=context.source_group,
            tool_name=context.tool_name,
            decision="execution_failed",
            reason="Host action descriptor is unavailable",
            request_id=context.request_id,
        )
    else:
        decision = evaluate_host_action_policy(action, context.gate, context.request_data)
        if not decision.allowed:
            if action.action_intent is not None:
                await deny_action_intent(
                    context.request_id,
                    reason=f"Policy denied after approval: {decision.reason}",
                )
            await asyncio.to_thread(
                write_ipc_response,
                ipc_response_path(context.source_group, context.request_id),
                {"error": f"Approved request blocked by current policy: {decision.reason}"},
            )
            await record_security_event(
                chat_jid=context.chat_jid,
                workspace=context.source_group,
                tool_name=context.tool_name,
                decision="execution_failed",
                reason=f"Policy changed before approved replay: {decision.reason}",
                request_id=context.request_id,
                capability_id=str(action.capability.id),
                action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
            )
            return
        if action.approval.mode is ApprovalMode.SESSION_TOOL:
            context.gate.grant_session_tool_approval(str(action.tool_name))
        try:
            context.request_data["source_group"] = context.source_group
            response = await execute_action_intent(
                action,
                context.request_data,
                request_id=context.request_id,
            )
            await asyncio.to_thread(
                write_ipc_response,
                ipc_response_path(context.source_group, context.request_id),
                response,
            )
            await record_security_event(
                chat_jid=context.chat_jid,
                workspace=context.source_group,
                tool_name=context.tool_name,
                decision="execution_failed" if "error" in response else "execution_succeeded",
                reason="handler returned an error" if "error" in response else None,
                request_id=context.request_id,
                capability_id=str(action.capability.id),
                action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
            )
            logger.info(
                "Approved request executed",
                request_id=context.request_id,
                tool_name=context.tool_name,
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - approved handler execution is an IPC boundary.
            logger.error(
                "Approved request failed",
                request_id=context.request_id,
                err=str(exc),
            )
            await asyncio.to_thread(
                write_ipc_response,
                ipc_response_path(context.source_group, context.request_id),
                {"error": f"Execution failed: {exc}"},
            )
            await record_security_event(
                chat_jid=context.chat_jid,
                workspace=context.source_group,
                tool_name=context.tool_name,
                decision="execution_failed",
                reason=type(exc).__name__,
                request_id=context.request_id,
                capability_id=str(action.capability.id),
                action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
            )


async def _execute_ipc_approval(
    request_data: dict[str, Any],
    source_group: str,
    request_id: str,
    deps: object | None,
) -> None:
    """Dispatch an approved IPC request through the registry.

    Sets _cop_approved=True on the request data so the handler skips
    the cop_gate call on re-entry (prevents infinite approval loops).
    Admin-only: host-mutating ops already passed admin checks before
    cop_gate was invoked.
    """
    if deps is None:
        logger.error(
            "Cannot dispatch IPC approval without deps",
            request_id=request_id,
        )
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(source_group, request_id),
            {"error": "Internal error: IPC approval missing deps"},
        )
        return

    try:
        request_data["_cop_approved"] = True
        await registry.dispatch(
            request_data, source_group, is_admin=True, deps=cast("IpcDeps", deps)
        )
        # Note: the IPC handler writes its own response file on success.
        # We write one here only on failure to ensure the container unblocks.
        logger.info(
            "Approved IPC request dispatched",
            request_id=request_id,
            task_type=request_data.get("type"),
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - approved IPC dispatch is an IPC boundary.
        logger.error(
            "Approved IPC request failed",
            request_id=request_id,
            err=str(exc),
        )
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(source_group, request_id),
            {"error": f"Execution failed: {exc}"},
        )
