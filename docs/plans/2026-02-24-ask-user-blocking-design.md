# Ask-User Blocking: Channel-Routed Agent Questions

**Date:** 2026-02-24
**Status:** Design

## Problem

When the agent needs to ask the user a clarifying question mid-task, the built-in
`AskUserQuestion` tool is useless in headless/`--print` mode — it completes
immediately with no way to deliver the answer back. The agent either skips the
question or gets an error, and continues without user input.

## Solution

Replace the built-in `AskUserQuestion` with a custom MCP tool that routes
questions through messaging channels (Slack Block Kit widgets, WhatsApp numbered
options) and blocks until the user responds — or until a configurable timeout.

## Existing Infrastructure: Approval Gate

The human approval gate (merged 2026-02-24) uses the same state-machine pattern:

- Container blocks (no response file written) via `_ipc_request.py` polling
- Host stores pending state as files in `ipc/{group}/pending_approvals/`
- User types `approve <id>` / `deny <id>` in chat
- Chat pipeline writes decision file to `approval_decisions/`
- IPC watcher picks up decision, executes or denies, writes response
- Container unblocks

Ask-user extends this pattern with richer UX (Block Kit widgets, multiple-choice)
and different semantics (options + free text vs binary approve/deny).

## Architecture

```
Container (MCP subprocess)          Host                          Channel (Slack/WhatsApp)
──────────────────────────          ────                          ────────────────────────
Claude calls ask_user MCP tool
  → writes task to ipc/tasks/
  → watchdog watches responses/  ──→ IPC watcher picks up task
    (blocks on asyncio.Event)       → stores file in pending_questions/
                                    → calls channel.send_ask_user() ──→ Block Kit widget
                                    │                                   (buttons + text input)
                                    │
                                    │  ... user responds ...
                                    │
                                    │                              ←── interaction callback
                                    ← on_ask_user_response()
                                    → writes ipc/responses/{id}.json
                                    → updates widget ("Answered: X")

  ← watchdog fires, Event set
  ← returns answer to Claude
```

### Late-Answer Path (Container Dead)

If the container was destroyed before the user responds:

1. Slack interaction arrives → host looks up pending question file
2. No live session → host cold-starts a new container
3. Answer is injected into `initial.json` as context:
   `"You previously asked: [question]. The user answered: [answer]. Continue."`
4. Session resumes via stored `session_id` — full conversation history is preserved

## Components

### 1. Container-Side MCP Tool

**File:** `container/agent_runner/src/agent_runner/agent_tools/_ask_user.py`

- New `ask_user` MCP tool with same semantics as `AskUserQuestion`
  (question text, options list, multiSelect)
- Uses watchdog `Observer` on `ipc/responses/` directory instead of polling
- `asyncio.Event` bridges watchdog thread → async MCP handler
- Configurable timeout cap (default 30 min), returns timeout message on expiry
- Cleans up observer in `finally` block

**Change:** `cores/claude.py` adds `"AskUserQuestion"` to `disallowed_tools`.

### 2. Host-Side Pending Question Store

**File:** `src/pynchy/security/pending_questions.py`

File-based state, matching the approval gate pattern (`pending_approvals/`):

- Stored in `ipc/{group}/pending_questions/{request_id}.json`
- Contains: request_id, short_id, questions payload, chat_jid, session_id,
  channel_name, message_id (Slack ts), timestamp
- Swept on startup (auto-expire stale questions, clean orphans)
- Mirrors `security/approval.py` structure (create, list, find_by_short_id, sweep)

### 3. Host-Side IPC Handler

**Registered as a separate IPC prefix:** `register_prefix("ask_user:", handler)`

Not routed through the service handler system — asking the user a question is
not a service write, so the security policy middleware does not apply.

Handler flow:
1. Parse task payload (request_id, questions)
2. Store PendingQuestion in DB
3. Resolve chat_jid and channel for the source group
4. Call `channel.send_ask_user(jid, request_id, questions)` → returns message_id
5. Store message_id in DB
6. Do NOT write to `ipc/responses/` — response comes later from channel callback

### 4. Channel Protocol Extension

**New optional method on `Channel`:**

```python
async def send_ask_user(
    self, jid: str, request_id: str, questions: list[dict]
) -> str | None:
    """Send an interactive question widget. Returns message_id."""
```

**Slack implementation:**
- Builds Block Kit payload: section block (question text), actions block
  (buttons for each option), input block (plain text for free-form answer)
- `request_id` embedded in `block_id` so interaction callbacks can be matched
- Registers `block_actions` handler for button clicks
- Registers `view_submission` or message action for text input submission

**WhatsApp fallback:**
- Sends numbered text options (e.g., "1. JWT tokens\n2. Session cookies\n3. Other")
- Incoming messages matched to pending questions by: pending question exists for
  this group + message is a valid option number or free-form text
- Matching logic lives in the WhatsApp channel plugin, not the message pipeline

**Unsupported channels:**
- `send_ask_user` not implemented → host writes error to `ipc/responses/`
  immediately so the agent isn't stuck

### 5. Answer Delivery

**Path A — Container alive:**
1. Channel interaction callback fires (Slack button click, WhatsApp reply)
2. Look up PendingQuestion by request_id
3. Write answer to `ipc/responses/{request_id}.json`
4. Container-side watchdog fires → MCP handler returns answer to Claude
5. Update channel widget (replace buttons with "Answered: X")
6. Delete pending question file

**Path B — Container dead (late answer):**
1. Same channel interaction callback fires
2. Look up pending question file — still exists
3. No live session for this group
4. Cold-start a new container via the normal `run_agent()` pipeline
5. Inject answer into initial context: prior question + user's answer
6. Session resumes via stored `session_id`
7. Update channel widget, delete pending question file

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Replace vs intercept built-in | Replace | Built-in completes immediately in headless mode; can't pause it |
| Container-side wait mechanism | Watchdog on `ipc/responses/` | MCP server is a separate subprocess; can't use the main process's input watcher (deadlock) |
| IPC routing | Separate prefix handler | Not a service write; security policy doesn't apply; keeps invariant clean |
| Timeout behavior | Cap + destroy container | 30-min default; user responds late → cold-start |
| Late-answer delivery | Cold-start immediately | Responsive UX; answer triggers agent execution like a regular message |
| Channel abstraction | Optional `send_ask_user` method | Channel-agnostic; Slack uses Block Kit, WhatsApp uses numbered text, unsupported channels return error |
| State persistence | File-based (`pending_questions/`) | Matches approval gate pattern; no schema migration; survives restarts |

## Out of Scope

- **Multi-question batching:** Claude's `AskUserQuestion` supports 1-4 questions
  per call. For v1, each question is a separate widget. Batching into a single
  Slack message with multiple action groups is a future enhancement.
- **Editing answers:** Once answered, the response is final. No "change my answer."
- **Approval gate convergence:** The approval gate and ask-user share the same
  state-machine pattern. A future refactor could extract a shared "pending human
  input" abstraction. For now, they are parallel implementations with different
  semantics.
