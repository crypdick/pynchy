## Idle Session Escape Hatch

If you are woken by system messages (deploy notifications, worktree notices, etc.) and determine there is nothing actionable to do, call `reset_context` with no message to end your session cleanly. Do NOT respond with "nothing to do" — that keeps the session active and you will get poked again on the next deploy. Calling `reset_context` without a message blanks the session so you stop getting woken by system events.
