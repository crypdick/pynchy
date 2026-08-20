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
- "ipc": dispatches through ipc._registry.dispatch() with a single-use,
  payload-bound approval receipt (host-mutating operations from cop_gate)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves approval runtime annotations.
)
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves approval decision paths at runtime.
)
from typing import Any, Protocol, cast

from pynchy.host.container_manager.ipc.approval_decision_context import (
    ApprovalDecision as _ApprovalDecision,
)
from pynchy.host.container_manager.ipc.approval_decision_context import (
    build_approval_decision_context as _build_approval_decision_context,
)
from pynchy.host.container_manager.ipc.approval_grants import apply_reusable_approval
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext as _ApprovalDecisionContext,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalReplayPolicy as _ApprovalReplayPolicy,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    approval_replay_gate,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    approval_replay_validation_error as _approval_replay_validation_error,
)
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001 - beartype resolves approval replay dependencies at runtime.
)
from pynchy.host.container_manager.ipc.file_claims import claim_ipc_file, release_ipc_file
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import approval as security_approval
from pynchy.host.container_manager.security.approval_binding import approval_binding_error
from pynchy.host.container_manager.security.approved_ipc import execute_approved_ipc
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import (
    ResolvedSecurityConfig,
    SecurityGate,
    SecuritySettings,
    build_workspace_security,
    evaluate_host_action_policy,
)
from pynchy.logger import logger
from pynchy.plugins.api import HostActionDescriptor  # noqa: TC001 - beartype resolves annotations.
from pynchy.workspace.api import (
    WorkspaceSecurity,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


class ApprovalSettings(SecuritySettings, Protocol):
    data_dir: Path

    def resolved_workspace_config(self, group_folder: str) -> ResolvedSecurityConfig | None: ...


def _unconfigured_settings() -> ApprovalSettings:
    raise RuntimeError("Approval configuration has not been composed")


_get_settings: Callable[[], ApprovalSettings] = _unconfigured_settings


def configure_approval_runtime(*, get_settings: Callable[[], ApprovalSettings]) -> None:
    """Bind the workspace policy source at host composition."""
    global _get_settings  # noqa: PLW0603 - one host process owns one configuration source.
    _get_settings = get_settings


def get_settings() -> ApprovalSettings:
    """Return the composed configuration source for approval replay."""
    return _get_settings()


@dataclass(frozen=True)
class _ApprovedServiceContext:
    request_data: dict[str, Any]
    source_group: str
    request_id: str
    tool_name: str
    chat_jid: str
    action: HostActionDescriptor | None
    gate: SecurityGate
    deps: IpcDeps
    approval_scope: str


def _read_json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _paths_match(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve()


def _unlink_all_missing_ok(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


async def process_approval_decision(
    decision_file: Path, source_group: str, *, deps: IpcDeps | None = None
) -> None:
    """Process an approval decision file — execute or deny the pending request."""
    if not claim_ipc_file(decision_file):
        return
    try:
        await _process_claimed_approval_decision(decision_file, source_group, deps=deps)
    finally:
        release_ipc_file(decision_file)


async def _process_claimed_approval_decision(  # noqa: PLR0911 - each invalid durable-state case has distinct cleanup semantics.
    decision_file: Path, source_group: str, *, deps: IpcDeps | None = None
) -> None:
    try:
        raw_decision = await asyncio.to_thread(_read_json_file, decision_file)
        decision = _ApprovalDecision.parse(raw_decision)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.error("Rejected invalid decision file", path=str(decision_file), err=str(exc))
        await asyncio.to_thread(decision_file.unlink, missing_ok=True)
        return

    s = get_settings()
    approval_group_dir = s.data_dir / "approvals" / source_group
    expected_decision_file = (
        approval_group_dir / "approval_decisions" / f"{decision.request_id}.json"
    )
    if not await asyncio.to_thread(_paths_match, decision_file, expected_decision_file):
        logger.warning(
            "Rejected decision outside host-owned approval state",
            path=str(decision_file),
            expected_path=str(expected_decision_file),
        )
        await asyncio.to_thread(decision_file.unlink, missing_ok=True)
        return

    # Find the corresponding pending approval
    pending_file = approval_group_dir / "pending_approvals" / f"{decision.request_id}.json"

    if not await asyncio.to_thread(pending_file.exists):
        logger.warning("No pending approval for decision", request_id=decision.request_id)
        await asyncio.to_thread(decision_file.unlink, missing_ok=True)
        return

    try:
        raw_pending = await asyncio.to_thread(security_approval.read_pending_approval, pending_file)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.error("Failed to read pending file", path=str(pending_file), err=str(exc))
        await asyncio.to_thread(_unlink_all_missing_ok, decision_file, pending_file)
        return
    if not isinstance(raw_pending, dict):
        logger.error("Rejected invalid pending approval", path=str(pending_file))
        await asyncio.to_thread(_unlink_all_missing_ok, decision_file, pending_file)
        return
    pending = raw_pending
    if (
        pending.get("request_id") != decision.request_id
        or pending.get("source_group") != source_group
    ):
        logger.error(
            "Rejected approval with mismatched pending identity",
            request_id=decision.request_id,
            source_group=source_group,
        )
        await asyncio.to_thread(decision_file.unlink, missing_ok=True)
        return

    binding_error = approval_binding_error(
        pending,
        cast("dict[str, Any]", raw_decision),
        request_id=decision.request_id,
        source_group=source_group,
    )
    if binding_error is not None:
        await _reject_invalid_approval_binding(
            pending,
            request_id=decision.request_id,
            source_group=source_group,
            reason=binding_error,
            deps=deps,
        )
        await asyncio.to_thread(_unlink_all_missing_ok, pending_file, decision_file)
        return

    def current_workspace_tools(group_folder: str) -> tuple[str, ...] | None:
        resolved = s.resolved_workspace_config(group_folder)
        return tuple(resolved.tools) if resolved is not None else None

    def configured_security(group_folder: str) -> WorkspaceSecurity | None:
        resolved = s.resolved_workspace_config(group_folder)
        return build_workspace_security(s, resolved) if resolved is not None else None

    replay_policy = _ApprovalReplayPolicy(
        configured_security=configured_security,
        workspace_tools=current_workspace_tools,
    )

    try:

        def current_replay_gate(
            *,
            require_resolved: bool,
            request_corruption_tainted: bool,
            request_secret_tainted: bool,
        ) -> SecurityGate | None:
            return approval_replay_gate(
                source_group,
                policy=replay_policy,
                require_resolved=require_resolved,
                request_corruption_tainted=request_corruption_tainted,
                request_secret_tainted=request_secret_tainted,
            )

        context = _build_approval_decision_context(
            pending,
            decision,
            source_group=source_group,
            replay_gate=current_replay_gate,
        )
    except (TypeError, ValueError) as exc:
        logger.error(
            "Rejected malformed pending approval",
            path=str(pending_file),
            err=str(exc),
        )
        await asyncio.to_thread(_unlink_all_missing_ok, decision_file, pending_file)
        return
    await _dispatch_approval_decision(context, deps, replay_policy)
    await asyncio.to_thread(_unlink_all_missing_ok, pending_file, decision_file)


async def _reject_invalid_approval_binding(
    pending: dict[str, Any],
    *,
    request_id: str,
    source_group: str,
    reason: str,
    deps: IpcDeps | None,
) -> None:
    if pending.get("handler_type") == "mcp_proxy":
        security_approval.resolve_mcp_proxy_approval(request_id, approved=False)
    if deps is not None:
        await deps.fail_action_intent(request_id, reason=reason)
    await asyncio.to_thread(
        write_ipc_response,
        ipc_response_path(source_group, request_id),
        {"error": f"Approval rejected: {reason}"},
    )
    await record_security_event(
        chat_jid=str(pending.get("approval_chat_jid") or "unknown"),
        workspace=source_group,
        tool_name=str(pending.get("tool_name") or "unknown"),
        decision="approval_binding_rejected",
        reason=reason,
        request_id=request_id,
    )


async def _dispatch_approval_decision(
    context: _ApprovalDecisionContext,
    deps: IpcDeps | None,
    replay_policy: _ApprovalReplayPolicy,
) -> None:
    if context.handler_type == "mcp_proxy":
        await _resolve_mcp_proxy_approval(context)
        return
    if context.approved:
        await _dispatch_approved_request(context, deps, replay_policy)
    else:
        await _dispatch_denied_request(context, deps)


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
    context: _ApprovalDecisionContext,
    deps: IpcDeps | None,
    replay_policy: _ApprovalReplayPolicy,
) -> None:
    validation_error: str | None
    if deps is None:
        validation_error = "Approval replay dependencies are unavailable."
    else:
        validation_error = await _approval_replay_validation_error(
            context,
            deps,
            replay_policy,
        )
    if validation_error is not None:
        if deps is not None:
            await deps.fail_action_intent(context.request_id, reason=validation_error)
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
    deps = cast("IpcDeps", deps)
    if not await apply_reusable_approval(context, deps):
        return
    if context.handler_type in {"security_bash", "security_artifact"}:
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(context.source_group, context.request_id),
            {
                "result": {
                    "decision": "allow",
                    "guarded_action_id": context.request_id,
                }
            },
        )
    elif context.handler_type == "ipc":
        await execute_approved_ipc(
            context.request_data,
            context.source_group,
            context.request_id,
            context.tool_name,
            deps,
        )
    else:
        if context.gate is None:
            raise RuntimeError("Approval replay policy disappeared after validation")
        if context.action is not None and context.action.action_intent is not None:
            await deps.approve_action_intent(
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
                deps=deps,
                approval_scope=context.approval_scope,
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


async def _dispatch_denied_request(context: _ApprovalDecisionContext, deps: IpcDeps | None) -> None:
    if deps is not None:
        await deps.deny_action_intent(context.request_id, reason="Denied by user")
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
        await context.deps.fail_action_intent(
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
                await context.deps.deny_action_intent(
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
        try:
            context.request_data["source_group"] = context.source_group
            response = await context.deps.execute_action_intent(
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
        except Exception as exc:  # noqa: BLE001 - approved handler execution is an IPC boundary.
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
