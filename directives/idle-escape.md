## Idle Session Escape Hatch

**This rule applies ONLY to messages clearly labeled as system messages** (deploy notifications, worktree notices, cron events, etc. — messages that are obviously machine-generated, not from a human). If you receive a system message and determine there is nothing actionable to do, call `reset_context` with no message to end your session cleanly. Do NOT respond with "nothing to do" — that keeps the session active and you will get poked again on the next event. Calling `reset_context` without a message blanks the session so you stop getting woken by system events.

**Never call `reset_context` in response to a user message.** If a human sends you a message and you have nothing useful to say, respond normally — do not reset the session. Resetting in response to a user message would silently discard the conversation, which is never the right behavior.
