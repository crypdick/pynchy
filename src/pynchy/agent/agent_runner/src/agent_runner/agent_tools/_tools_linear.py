"""Host-owned Linear work-item lifecycle tools."""

from __future__ import annotations

from ._registry import register_ipc_tool


def _issue_schema(*, include_status: bool = False) -> dict[str, object]:
    properties: dict[str, object] = {"issue_id": {"type": "string", "minLength": 1}}
    required = ["issue_id"]
    if include_status:
        properties["status"] = {
            "type": "string",
            "enum": ["agent_proposed"],
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
    name="linear_create_requested_todo",
    description=(
        "Create a Ready for Planning Linear item only when the current direct human message "
        "explicitly requests the work. Quote that full message exactly. This authorizes planning, "
        "not execution; use linear_create_todo for an agent-originated proposal."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "description": {"type": "string"},
            "exact_description": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true only when the current direct human explicitly requires the supplied "
                    "description byte-for-byte. This omits issue-body workspace provenance; never "
                    "use it for an agent-originated proposal."
                ),
            },
            "priority": {
                "type": "integer",
                "enum": [0, 1, 2, 3, 4],
                "description": "Linear priority: 0 none, 1 urgent, 2 high, 3 medium, 4 low.",
            },
            "authorization_quote": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The full current direct human message that requests this work, copied exactly."
                ),
            },
        },
        "required": ["title", "authorization_quote"],
        "additionalProperties": False,
    },
)


register_ipc_tool(
    name="linear_submit_plan",
    description=(
        "Persist a concrete Markdown plan for a Ready for Planning Linear item and move it "
        "to Awaiting Plan Approval. This does not authorize or begin execution."
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
    name="linear_await_review_work_item",
    description=(
        "Report a completed Linear work item for human acceptance. This moves linked or "
        "already-completed unlinked work to Awaiting Review with a summary and optional "
        "evidence. Include a GitHub pull request URL only when the work produced one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "pull_request_url": {
                "type": "string",
                "format": "uri",
                "pattern": "^https://github\\.com/[^/]+/[^/]+/pull/[1-9][0-9]*/?$",
                "description": "Optional canonical pull request URL for development work.",
            },
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
        "Return an unlinked workspace Linear item to Agent Proposed. Use linear_submit_plan "
        "to persist a plan and request plan approval. Human decision states remain controlled "
        "in Linear, and active Pynchy executions must use lifecycle tools."
    ),
    input_schema=_issue_schema(include_status=True),
)
