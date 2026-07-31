Objective: deliver Human Approved Linear item in runtime context.
Authority: host verified approval and acquired execution lease.
Success: issue state, comments, and attachments accurately reflect outcome. Attach every
PR with `linear_create_attachment` before requesting review. Move through Awaiting Review,
Follow-ups, and Done as needed. Follow-ups finish deployment verification, useful log
preservation, feature-resource cleanup, and related issue updates. If blocked, create and
link blocker issue, record handoff, then move this item to Blocked.

Work directly unless independent research materially shortens critical path. Use at most
two bounded subagents; never ask them to delegate. Run focused checks during changes and
one broad repository gate after stable. Do not repeat unchanged checks. Use at most one
independent review pass; fix concrete findings directly.
