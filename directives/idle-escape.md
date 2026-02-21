## Idle Session Escape Hatch

**Never call `reset_context` in response to a user message** unless they explicitly ask for a reset. Resetting unprompted would silently discard the conversation.

### System messages (deploy notices, worktree notices, cron events, etc.)

Deploy notices mean files in your workspace may have changed. You do not need to act unless you hit a conflict or error. Treat them as informational.

**When to reset:** Call `reset_context` with no message only if the last 5+ messages are all system messages with zero user input between them. Do NOT respond with "nothing to do" — that keeps the session alive and you'll get poked again.

**When not to reset:** If the conversation has any recent user input, do not reset — the session is still active regardless of system messages arriving.
