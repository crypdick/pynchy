"""Typed host-action registration for the built-in Linear work-item lifecycle."""

from __future__ import annotations

from pynchy.actions.api import ActionId
from pynchy.plugins.api import (
    ActionIntentContract,
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.plugins.integrations.linear_comment_actions import (
    handle_create_comment,
    linear_comment_action_draft,
    linear_comment_action_receipt,
)
from pynchy.plugins.integrations.linear_work_items import (
    handle_list_work_items,
    handle_move_todo,
    handle_reconcile_work_item,
    handle_submit_plan,
)

_ActionSpec = tuple[str, str, str, HostActionAccess, HostActionHandler]


def host_action_registration() -> HostActionRegistration:
    """Return lifecycle actions with one shared Linear trust-policy service."""
    return HostActionRegistration(actions=tuple(_descriptor(spec) for spec in _action_specs()))


def _action_specs() -> tuple[_ActionSpec, ...]:
    return (
        (
            "linear_create_comment",
            "linear.comment.create",
            "Add a workspace-owned Linear comment without reopening its own conversation.",
            HostActionAccess.WRITE,
            handle_create_comment,
        ),
        (
            "linear_submit_plan",
            "linear.todo.plan",
            "Persist or revise a concrete Linear plan for human approval.",
            HostActionAccess.WRITE,
            handle_submit_plan,
        ),
        (
            "linear_list_work_items",
            "linear.workitem.list",
            "List durable Pynchy executions linked to Linear work items.",
            HostActionAccess.READ,
            handle_list_work_items,
        ),
        (
            "linear_reconcile_work_item",
            "linear.workitem.reconcile",
            (
                "Reconcile an uncertain or reviewed conflicted Linear work-item transition "
                "from provider state."
            ),
            HostActionAccess.WRITE,
            handle_reconcile_work_item,
        ),
        (
            "linear_move_todo",
            "linear.todo.move",
            "Move a Linear item and durably record a typed linked-work outcome.",
            HostActionAccess.WRITE,
            handle_move_todo,
        ),
    )


def _descriptor(spec: _ActionSpec) -> HostActionDescriptor:
    tool_name, action_id, summary, access, handler = spec
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="linear",
            summary=summary,
            action_ids=(ActionId(action_id),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name="linear",
                    description="Enable the Linear integration for this workspace.",
                ),
            ),
            documentation="docs/integrations/linear.md",
        ),
        tool_name=HostToolName(tool_name),
        handler=handler,
        access=access,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
        policy_service="linear",
        action_intent=(
            ActionIntentContract(
                provider="linear",
                draft_from_request=linear_comment_action_draft,
                receipt_from_response=linear_comment_action_receipt,
            )
            if action_id == "linear.comment.create"
            else None
        ),
    )
