"""Host-owned Linear work-item lifecycle tools."""

from __future__ import annotations

from ._registry import register_ipc_tool


def _issue_schema(*, include_status: bool = False) -> dict[str, object]:
    properties: dict[str, object] = {"issue_id": {"type": "string", "minLength": 1}}
    required = ["issue_id"]
    if include_status:
        properties["status"] = {
            "type": "string",
            "enum": ["agent_proposed", "awaiting_plan_approval"],
        }
        required.append("status")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _evidence_refs_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Optional concise URLs, artifact paths, or other evidence references.",
    }


register_ipc_tool(
    name="linear_list_work_items",
    description=(
        "List durable Pynchy execution records for Linear work items in this workspace. "
        "Use it to see ownership, task/turn linkage, lifecycle state, and blockers."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)

register_ipc_tool(
    name="linear_claim_work_item",
    description=(
        "Claim one Human Approved workspace Linear item for the current Pynchy execution and "
        "move it to In Progress. Planning readiness does not authorize execution. A claim is "
        "rejected if another active Pynchy execution owns it."
    ),
    input_schema=_issue_schema(),
)

register_ipc_tool(
    name="linear_complete_work_item",
    description=(
        "Complete a Linear work item owned by the current workspace's active Pynchy "
        "execution. Include a concise completion summary."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "evidence_refs": _evidence_refs_schema(),
        },
        "required": ["issue_id", "summary"],
        "additionalProperties": False,
    },
)

register_ipc_tool(
    name="linear_block_work_item",
    description=(
        "Move a Pynchy-owned Linear work item to Blocked and record the concrete blocker."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "evidence_refs": _evidence_refs_schema(),
        },
        "required": ["issue_id", "reason"],
        "additionalProperties": False,
    },
)

register_ipc_tool(
    name="linear_handoff_work_item",
    description=(
        "Mark a Pynchy-owned Linear work item Blocked and hand it to a named owner. "
        "The Pynchy execution becomes terminal so the next owner may claim it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "minLength": 1},
            "owner": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "evidence_refs": _evidence_refs_schema(),
        },
        "required": ["issue_id", "owner"],
        "additionalProperties": False,
    },
)

register_ipc_tool(
    name="linear_reconcile_work_item",
    description=(
        "Resolve a work item whose previous Linear transition has an unknown provider outcome. "
        "This checks Linear rather than retrying the write blindly."
    ),
    input_schema=_issue_schema(),
)

register_ipc_tool(
    name="linear_move_todo",
    description=(
        "Move an unlinked workspace Linear item between agent-controlled planning states. "
        "This tool cannot set Ready for Planning, Human Approved, or Rejected; those states "
        "record human decisions in Linear. Active Pynchy executions must use lifecycle tools."
    ),
    input_schema=_issue_schema(include_status=True),
)
