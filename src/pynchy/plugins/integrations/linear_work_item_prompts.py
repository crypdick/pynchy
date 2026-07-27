"""Host-owned execution contracts for managed Linear work."""

from __future__ import annotations

EXECUTION_CONTRACT = (
    "Objective: deliver the Human Approved Linear work item below.\n"
    "Authority: the host verified approval and acquired the execution lease.\n"
    "Success: leave the issue state, comments, and attachments accurately reflecting the "
    "outcome. If the work produces pull requests, attach every PR to the issue with "
    "linear_create_attachment before requesting review. Use your judgment to move the issue "
    "through Awaiting Review, Follow-ups, and Done. Follow-ups are for final operational work "
    "such as deployment verification, preserving useful logs before teardown, cleaning feature "
    "resources, and updating or unblocking related issues."
)
FOLLOW_UPS_CONTRACT = (
    "Objective: finish the Follow-ups for the Linear work item below.\n"
    "Authority: the underlying work was already Human Approved.\n"
    "Success: use your judgment to finish the whole job. Verify delivery or deployment, "
    "preserve useful logs before teardown, clean feature resources, and update or unblock "
    "related issues when relevant. Record useful context and move the item to Done when it is "
    "genuinely finished, or Blocked when it needs outside help."
)
