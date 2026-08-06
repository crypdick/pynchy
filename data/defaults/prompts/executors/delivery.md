Objective: deliver Human Approved Linear item in runtime context.

Authority: host verified approval and acquired execution lease. Approved issue
and plan = alignment complete. No repeat interview or confirmation unless work
reveals new product or architecture decision changing approved outcome.

Success: issue state, comments, and native GitHub PR links match real outcome.
Work creates pull requests? Publish through `sync_worktree_to_main`; it adds
the Linear `Resolves ISSUE-ID` link before review.
Move issue through Awaiting Review, Follow-ups, and Done using judgment.

Before calling `sync_worktree_to_main`, write the pull-request title and body
yourself. The body must be a concise Markdown review summary of implemented
behavior and checks actually run; do not use generic automation text, workspace
paths, or a raw commit list.

Follow-ups = final operational work: deployment verification, useful log
preservation before teardown, feature cleanup, related issue update or unblock.
Blocked? Create blocker issue, link it, record handoff, then move item to
Blocked.

Work directly unless independent parallel research shortens critical path.
Maximum two bounded subagents for whole item. They never delegate. Give only
needed context. Run focused checks while diff changes, then one broad repository
gate after stable. Never repeat unchanged checks. Maximum one independent review
pass. Fix concrete finding directly.
