# Context Reset via Magic Words

## Context

The agent currently has no real "context reset" — typing "context reset" just gets forwarded to the agent, which roleplays a reset without actually clearing anything (the session ID persists, so Claude SDK resumes the same conversation). We need host-side interception of reset phrases so the session is genuinely cleared.

## Design

**Pattern matching**: Catch natural speech variants — `{reset, restart} + {context}` in either order, case-insensitive, as standalone messages. Regex: `^\s*(reset|restart)\s+(context|session)|(context|session)\s+(reset|restart)\s*$`

**Intercept point**: `app.py:_process_group_messages()`, after fetching messages but before formatting/sending to container. If the *last* message in the batch matches the pattern, trigger reset. Earlier messages in the batch are discarded (they're pre-reset context).

**Reset actions**:
1. Clear `self.sessions[group.folder]` (set to `None`)
2. Delete session row from DB
3. If there's an active container for this group, close it via `close_stdin()`
4. Send a system confirmation to the user (plain text for now)
5. Advance the message cursor (so reset messages aren't re-processed)
6. Return early — don't run the agent

## Files to modify

### `src/pynchy/config.py`
- Add `CONTEXT_RESET_PATTERN` regex constant

### `src/pynchy/db.py`
- Add `clear_session(group_folder)` — DELETE from sessions table

### `src/pynchy/app.py`
- In `_process_group_messages()`: after fetching `missed_messages`, check if the last message matches `CONTEXT_RESET_PATTERN`. If yes:
  - Clear session via `clear_session()` + `self.sessions.pop(group.folder, None)`
  - Close any active container via `self.queue.close_stdin(chat_jid)`
  - Send confirmation via channel
  - Advance cursor, save state, return early

## Verification
- Send "reset context" in WhatsApp → should get system confirmation, no agent invocation
- Send "Context Restart" → same behavior
- Send follow-up message → agent starts fresh session (new session ID in logs)
- Send "please reset the context of our discussion" → should NOT trigger (not a standalone reset phrase)
