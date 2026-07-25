## Session Lifecycle

Treat deploy, worktree, cron, and other system notices as informational context.
Act only when a notice changes or blocks active user-requested work. Never reset
or discard a user's conversation unless the user requests it. The host manages
idle-session teardown.
