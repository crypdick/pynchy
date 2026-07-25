"""Standard Pynchy todo statuses for Linear."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TodoStatusSpec:
    name: str
    type: str
    position: float
    color: str


# Authorization is an explicit workflow state, not an inference from issue content.
AGENT_PROPOSED_STATUS = "agent_proposed"
HUMAN_APPROVED_STATUS = "human_approved"
AWAITING_REVIEW_STATUS = "awaiting_review"
FOLLOW_UPS_STATUS = "follow_ups"
AGENT_SETTABLE_STATUSES = frozenset(
    {
        AGENT_PROPOSED_STATUS,
        AWAITING_REVIEW_STATUS,
        FOLLOW_UPS_STATUS,
        "blocked",
        "done",
    }
)
HUMAN_SETTABLE_STATUSES = frozenset(
    {
        HUMAN_APPROVED_STATUS,
        "rejected",
    }
)
TOOL_SETTABLE_STATUSES = AGENT_SETTABLE_STATUSES | HUMAN_SETTABLE_STATUSES
TERMINAL_STATE_TYPES = frozenset({"completed", "canceled"})

# NOTE: Keep docs/integrations/linear.md, linear_tools.py, and the agent-runner
# Linear tool schemas in sync.
LINEAR_TODO_STATUSES: dict[str, TodoStatusSpec] = {
    AGENT_PROPOSED_STATUS: TodoStatusSpec("Agent Proposed", "backlog", 10.0, "#8A8F98"),
    HUMAN_APPROVED_STATUS: TodoStatusSpec("Human Approved", "unstarted", 20.0, "#56CCF2"),
    "in_progress": TodoStatusSpec("In Progress", "started", 30.0, "#2F80ED"),
    AWAITING_REVIEW_STATUS: TodoStatusSpec("Awaiting Review", "started", 40.0, "#5E6AD2"),
    FOLLOW_UPS_STATUS: TodoStatusSpec("Follow-ups", "started", 50.0, "#F2994A"),
    "blocked": TodoStatusSpec("Blocked", "started", 60.0, "#EB5757"),
    "done": TodoStatusSpec("Done", "completed", 70.0, "#27AE60"),
    "rejected": TodoStatusSpec("Rejected", "canceled", 80.0, "#6B7280"),
}
