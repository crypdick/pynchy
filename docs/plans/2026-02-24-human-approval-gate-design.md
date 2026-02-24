# Human Approval Gate Design

Implements the enforcement layer for the lethal trifecta defense. When the
security policy middleware sets `needs_human=True`, the host holds the request,
notifies the user, and waits for an explicit approve/deny decision before
executing the action.

## Context

The security middleware (`security/middleware.py`) already evaluates every
service write and sets `needs_human=True` when:

- `dangerous_writes=True` on the target service, OR
- The full trifecta: corruption-tainted + secret-tainted + public sink, OR
- The outbound payload contains detected secrets

Currently the `needs_human` branch in `_handlers_service.py:176` writes an
immediate error response: `"Human approval required (TODO: not yet implemented)"`.
This design replaces that stub.

## Architecture: File-Backed State Machine

Three file types, one watchdog-monitored directory tree:

```
ipc/{group}/pending_approvals/{request_id}.json   <- PENDING state
ipc/{group}/approval_decisions/{request_id}.json   <- transition event
ipc/{group}/responses/{request_id}.json            <- final response (existing)
```

### State transitions

```
              request arrives
              (needs_human=True)
                    |
                    v
              .----------.
              | PENDING  |  <- pending_approvals/{id}.json written
              '----+-----'     notification broadcast to chat
                   |
          .--------+---------.
          |        |         |
     "approve"  "deny"    stale
       <id>      <id>    (startup
          |        |      sweep)
          v        v         v
      APPROVED   DENIED   EXPIRED
          |        |         |
          v        +----+----'
    execute action      |
          |             v
          v       write error
    write result   response
     response
```

| From | Trigger | File action | Side effects |
|------|---------|-------------|--------------|
| -- -> PENDING | `needs_human=True` | Write pending file | Broadcast notification, audit log |
| PENDING -> APPROVED | `approve <id>` | Write decision file (approved=true) | -- |
| PENDING -> DENIED | `deny <id>` | Write decision file (approved=false) | -- |
| APPROVED -> done | Watchdog picks up decision | Execute request, write response, delete pending+decision | Audit log |
| DENIED -> done | Watchdog picks up decision | Write error response, delete pending+decision | Audit log |
| PENDING -> EXPIRED | Startup sweep | Write error response, delete pending | Audit log |

### Why files

- **Durable**: survives crashes. On restart, startup sweep handles orphaned
  pending files.
- **Consistent**: reuses the existing IPC file + watchdog pattern already
  proven in `_watcher.py`.
- **Non-blocking**: the service handler writes the pending file and returns
  immediately. No coroutine held open, no timeout management.
- **Decoupled**: the chat pipeline writes a decision file; the IPC watcher
  picks it up. No shared in-memory state between the two.

### Container-side wait

The container's `_ipc_request.py` already waits up to 300 seconds for
`responses/{request_id}.json` to appear. The host simply delays writing
that file until the user approves or denies. Zero container-side changes.

If the user takes longer than 5 minutes, the container times out naturally
and the agent gets a timeout error. The pending approval file remains on
disk; if the user later approves, the response file is written harmlessly
(the container has moved on).

## Components

### 1. Approval state manager (`security/approval.py`, new)

Handles the PENDING state:

- `create_pending_approval(request_id, tool_name, source_group, chat_jid, request_data)`:
  writes `pending_approvals/{request_id}.json`.
- `list_pending_approvals(group=None)`: lists pending files, optionally filtered
  by group. Used by the `pending` chat command.
- `sweep_expired_approvals()`: finds pending files older than 5 minutes, writes
  error responses, deletes pending files, records audit events. Called on startup
  and optionally on a slow timer.

Pending file schema:

```json
{
  "request_id": "a7f3b2c1d4e5f6a7...",
  "short_id": "a7f3b2c1",
  "tool_name": "x_post",
  "source_group": "personal",
  "chat_jid": "...",
  "request_data": { "...full original request..." },
  "timestamp": "2026-02-24T12:00:00Z",
  "notification_sent": true
}
```

The `short_id` is the first 8 hex chars of the request_id. Used for
user-facing display and command input.

### 2. Approval command interceptor (`chat/commands.py` + `chat/message_handler.py`)

New matchers:

- `is_approval_command(text) -> tuple[str, str] | None`:
  detects `approve <id>` or `deny <id>`. Returns `(action, short_id)` or None.
- `is_pending_query(text) -> bool`: detects `pending`.

Integration in `intercept_special_command()`, before the `!` command check:

```python
approval = is_approval_command(content)
if approval:
    action, short_id = approval
    await handle_approval_command(deps, chat_jid, group, action, short_id, message)
    return True

if is_pending_query(content):
    await handle_pending_query(deps, chat_jid)
    return True
```

The `handle_approval_command` function:

1. Finds the pending file matching `short_id` (glob `pending_approvals/{short_id}*.json`)
2. If not found: broadcasts "no pending request with that ID"
3. If found: writes `approval_decisions/{request_id}.json` with decision + approver

Decision file schema:

```json
{
  "request_id": "a7f3b2c1d4e5f6a7...",
  "approved": true,
  "decided_by": "ricardo",
  "decided_at": "2026-02-24T12:01:30Z"
}
```

### 3. Decision handler (IPC watcher extension)

Register a watchdog handler for `approval_decisions/` directories. When a
decision file appears:

1. Read decision file
2. Find corresponding pending file by request_id
3. If approved:
   - Re-resolve the plugin handler for the tool
   - Execute the original request
   - Write `responses/{request_id}.json` with the result
4. If denied:
   - Write `responses/{request_id}.json` with `{"error": "Denied by user"}`
5. Record audit event (`decision="approved"` or `decision="denied_by_user"`)
6. Delete both pending and decision files

This handler reuses the same plugin dispatch path as `_handlers_service.py`
(call `_get_plugin_handlers()`, look up tool_name, call handler with
request_data). The policy check is skipped on execution since the human
already approved.

### 4. Startup sweep (IPC watcher init)

On startup, `_sweep_existing()` already handles orphaned IPC files. Extend
it to also sweep `pending_approvals/`:

- For each pending file older than 5 minutes: auto-deny (write error
  response, delete pending file, audit log).
- For each pending file still fresh: re-broadcast notification to chat
  (the user may have missed it during the restart).
- For each orphaned decision file (no matching pending): delete it.

### 5. Integration in service handler

Replace the stub at `_handlers_service.py:176`:

```python
if decision.needs_human:
    await record_security_event(
        chat_jid=chat_jid, workspace=source_group,
        tool_name=tool_name, decision="approval_requested",
        corruption_tainted=policy.corruption_tainted,
        secret_tainted=policy.secret_tainted,
        reason=decision.reason, request_id=request_id,
    )
    create_pending_approval(
        request_id=request_id, tool_name=tool_name,
        source_group=source_group, chat_jid=chat_jid,
        request_data=data,
    )
    await deps.broadcast_to_channels(chat_jid, format_approval_notification(
        tool_name=tool_name, request_data=data, short_id=request_id[:8],
    ))
    # No response file written -- container blocks until user decides
    return
```

## Notification format

```
🔐 Approval required

Workspace: personal
Action: x_post
Details: post "Meeting reminder for..."

→ approve a7f3b2c1  /  deny a7f3b2c1
```

Sanitization rules for the details line:
- Show tool name and key arguments
- Truncate values longer than 100 chars
- Redact fields that look like secrets (reuse `secrets_scanner`)
- Omit internal fields (`type`, `request_id`, `source_group`)

## User commands

| Command | Effect |
|---------|--------|
| `approve <short_id>` | Approve the pending request |
| `deny <short_id>` | Deny the pending request |
| `pending` | List all pending approval requests with summaries |

These are bare words (no `!` prefix), intercepted in
`intercept_special_command()` before the `!` command branch. The short_id
(8 hex chars) is specific enough to avoid collisions with normal conversation.

## Scope boundary

This design covers the host-side approval state machine only. Out of scope:

- **Container-side watchdog for response polling**: the container's
  `_ipc_request.py` currently polls every 0.5s. Replacing this with watchdog
  is a good improvement but independent of the approval system.
- **Deputy agent scanning** (Step 7): the `needs_deputy` flag is orthogonal.
  The deputy scans untrusted input on reads; the approval gate enforces on
  writes. They compose but don't depend on each other.
- **Multi-channel approval**: notifications go to the group's chat channel.
  Supporting approval via a different channel (e.g., dedicated admin channel)
  is a future enhancement.

## Testing

- Unit tests for `approval.py`: create, list, sweep, file schemas
- Unit tests for command matchers: `is_approval_command`, `is_pending_query`
- Integration test: full flow from `needs_human=True` through pending file,
  decision file, to response file
- Edge cases: expired approval, unknown short_id, duplicate approve,
  approve after container timeout, crash recovery sweep
