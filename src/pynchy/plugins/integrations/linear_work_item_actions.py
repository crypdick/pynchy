"""Typed host-action registration for the built-in Linear work-item lifecycle."""

from __future__ import annotations

from pynchy.actions import ActionId
from pynchy.capabilities import (
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
from pynchy.plugins.integrations.linear_work_items import (
    handle_await_review_work_item,
    handle_block_work_item,
    handle_claim_work_item,
    handle_create_requested_todo,
    handle_handoff_work_item,
    handle_list_work_items,
    handle_move_unlinked_todo,
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
            "linear_create_requested_todo",
            "linear.todo.request",
            "Create planning work from an explicit request in the current direct user turn.",
            HostActionAccess.WRITE,
            handle_create_requested_todo,
        ),
        (
            "linear_list_work_items",
            "linear.workitem.list",
            "List durable Pynchy executions linked to Linear work items.",
            HostActionAccess.READ,
            handle_list_work_items,
        ),
        (
            "linear_submit_plan",
            "linear.todo.plan",
            "Persist a concrete Linear plan and request human plan approval.",
            HostActionAccess.WRITE,
            handle_submit_plan,
        ),
        (
            "linear_claim_work_item",
            "linear.workitem.claim",
            "Claim a Human Approved Linear work item for the current Pynchy execution.",
            HostActionAccess.WRITE,
            handle_claim_work_item,
        ),
        (
            "linear_await_review_work_item",
            "linear.workitem.review",
            "Submit completed linked or existing Linear work for human review.",
            HostActionAccess.WRITE,
            handle_await_review_work_item,
        ),
        (
            "linear_block_work_item",
            "linear.workitem.block",
            "Mark a Pynchy-linked Linear work item blocked with a reason.",
            HostActionAccess.WRITE,
            handle_block_work_item,
        ),
        (
            "linear_handoff_work_item",
            "linear.workitem.handoff",
            "Hand off a Pynchy-linked Linear work item to another owner.",
            HostActionAccess.WRITE,
            handle_handoff_work_item,
        ),
        (
            "linear_reconcile_work_item",
            "linear.workitem.reconcile",
            "Reconcile an uncertain Linear work-item transition from provider state.",
            HostActionAccess.WRITE,
            handle_reconcile_work_item,
        ),
        (
            "linear_move_todo",
            "linear.todo.move",
            "Return an unlinked Linear item to Agent Proposed.",
            HostActionAccess.WRITE,
            handle_move_unlinked_todo,
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
    )
