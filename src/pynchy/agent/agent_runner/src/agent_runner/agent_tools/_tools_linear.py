"""Host-enforced Linear authority and execution-ownership tools."""

from __future__ import annotations

from ._registry import register_ipc_tool


def _issue_schema(*, include_status: bool = False) -> dict[str, object]:
    properties: dict[str, object] = {"issue_id": {"type": "string", "minLength": 1}}
    required = ["issue_id"]
    if include_status:
        properties["status"] = {
            "type": "string",
            "enum": [
                "agent_proposed",
                "human_approved",
                "awaiting_review",
                "follow_ups",
                "blocked",
                "done",
                "rejected",
            ],
        }
        required.append("status")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


register_ipc_tool(
    name="linear_submit_plan",
    description=(
        "Persist a concrete Markdown plan for a Ready for Planning Linear item, or revise its "
        "existing plan while it is Awaiting Plan Approval. The item remains Awaiting Plan "
        "Approval; this does not authorize or begin execution."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "minLength": 1},
            "plan": {"type": "string", "minLength": 1},
        },
        "required": ["issue_id", "plan"],
        "additionalProperties": False,
    },
)

register_ipc_tool(
    name="linear_list_work_items",
    description="List durable Linear execution leases and outcomes for this workspace.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
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
        "Move a workspace item to another state using your judgment about the work's "
        "actual lifecycle. Human Approved and Rejected require a current direct-human "
        "instruction; In Progress is host-managed. You may move completed work through "
        "Awaiting Review, Follow-ups, and Done yourself."
    ),
    input_schema=_issue_schema(include_status=True),
)
