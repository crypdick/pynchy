"""Agent instructions for authenticated Linear issue activity."""

from __future__ import annotations

LINEAR_ISSUE_INSTRUCTIONS = (
    "The Linear issue bound to this thread changed. Read its current state and take "
    "appropriate action. If independent verification shows the requested outcome already "
    "exists, call linear_await_review_work_item with a summary and evidence; an earlier claim "
    "or pull request is not required."
)


def comment_instructions(action: str) -> str:
    """Describe a comment event and the verified-existing-work reconciliation path."""
    activity = {
        "create": "A new comment was posted",
        "update": "A comment was edited",
        "remove": "A comment was removed",
    }[action]
    return (
        f"{activity} on the Linear issue bound to this thread. Read it and take appropriate "
        "action under the issue's current workflow state. If independent verification shows "
        "the requested outcome already exists, call linear_await_review_work_item with a "
        "summary and evidence; an earlier claim or pull request is not required."
    )
