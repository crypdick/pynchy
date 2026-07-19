"""Standard Pynchy todo statuses for Linear."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TodoStatusSpec:
    name: str
    type: str
    position: float
    color: str


# Authorization must be visible in the workflow name. In particular, planning
# readiness must never be mistaken for human permission to execute.
AGENT_PROPOSED_STATUS = "agent_proposed"
READY_FOR_PLANNING_STATUS = "ready_for_planning"
AWAITING_PLAN_APPROVAL_STATUS = "awaiting_plan_approval"
HUMAN_APPROVED_STATUS = "human_approved"
AWAITING_REVIEW_STATUS = "awaiting_review"
AGENT_SETTABLE_STATUSES = frozenset(
    {
        AGENT_PROPOSED_STATUS,
    }
)
TERMINAL_STATE_TYPES = frozenset({"completed", "canceled"})

# NOTE: Keep docs/integrations/linear.md and the agent-runner Linear tool schemas in sync.
LINEAR_TODO_STATUSES: dict[str, TodoStatusSpec] = {
    AGENT_PROPOSED_STATUS: TodoStatusSpec("Agent Proposed", "backlog", 10.0, "#8A8F98"),
    READY_FOR_PLANNING_STATUS: TodoStatusSpec("Ready for Planning", "unstarted", 20.0, "#F2C94C"),
    AWAITING_PLAN_APPROVAL_STATUS: TodoStatusSpec(
        "Awaiting Plan Approval", "unstarted", 30.0, "#BB87FC"
    ),
    HUMAN_APPROVED_STATUS: TodoStatusSpec("Human Approved", "unstarted", 40.0, "#56CCF2"),
    "in_progress": TodoStatusSpec("In Progress", "started", 50.0, "#2F80ED"),
    AWAITING_REVIEW_STATUS: TodoStatusSpec("Awaiting Review", "started", 55.0, "#5E6AD2"),
    "blocked": TodoStatusSpec("Blocked", "started", 60.0, "#EB5757"),
    "done": TodoStatusSpec("Done", "completed", 70.0, "#27AE60"),
    "rejected": TodoStatusSpec("Rejected", "canceled", 80.0, "#6B7280"),
}
