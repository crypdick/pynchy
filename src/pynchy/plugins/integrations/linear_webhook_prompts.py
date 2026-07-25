"""Agent instructions for authenticated Linear issue activity."""

from __future__ import annotations

LINEAR_ISSUE_INSTRUCTIONS = (
    "Handle this Linear issue update within its current authorization. Use your judgment about "
    "the work's actual state instead of treating workflow names as a ritual. When authorized "
    "work produces pull requests, attach every PR to this issue with linear_create_attachment "
    "before requesting review. If the issue entered Follow-ups, finish the operational loose "
    "ends that matter: verify deployment or delivery, preserve useful logs before teardown, "
    "clean feature resources, and update or unblock related issues as appropriate. Move it to "
    "Done when the whole job is genuinely finished, or Blocked when it needs outside help."
)


def comment_instructions(action: str) -> str:
    """Describe a comment event and the verified-existing-work reconciliation path."""
    activity = {
        "create": "A new comment was posted",
        "update": "A comment was edited",
        "remove": "A comment was removed",
    }[action]
    return (
        f"{activity} on this Linear issue. Respond within its current authorization and use "
        "your judgment to keep the issue, comments, attachments, and follow-up work accurate."
    )
