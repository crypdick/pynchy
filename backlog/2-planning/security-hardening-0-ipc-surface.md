# Security Hardening: Step 0 - Reduce IPC Surface

## Summary

Shrink the container-to-host IPC channel from an arbitrary-payload pipe to a narrow signal-based protocol. Containers raise signals; the host decides what to do. Data-carrying requests go through Deputy mediation. Also replace the polling loop with inotify for event-driven processing.

This is a transport-level hardening that complements the policy middleware (Step 2) and human approval gate (Step 6). Those steps enforce policy on IPC requests; this step reduces what can be requested in the first place.

## Current implementation status

**Done (since plan was written):**
- Polling replaced with watchdog/inotify (`src/pynchy/ipc/_watcher.py`)
- Startup sweep for crash recovery (`_sweep_existing()`)
- Signal validation via `_protocol.py` (Tier 1 signals + Tier 2 requests with `request_id`)
- IPC registry with handler dispatch (`_registry.py`)

**Still TODO:**
- Signal-only conversion for Tier 1 types (send_message, reset_context, refresh_groups)
- Deputy mediation for Tier 2 data-carrying requests
- send_message elimination (host reads SDK output instead)

## Context

### Current state

Containers write JSON payloads to `{data_dir}/ipc/{group}/tasks/`. The host watches via `watchdog` (inotify on Linux, FSEvents on macOS) and dispatches to registered handlers via `_registry.py`.

Key files:
- `container/agent_runner/src/agent_runner/ipc_mcp.py` — MCP tools that write IPC files (container side)
- `src/pynchy/ipc/_watcher.py` — Watchdog-based event loop that processes IPC files (host side)
- `src/pynchy/ipc/_registry.py` — Handler registration and dispatch
- `src/pynchy/ipc/_protocol.py` — Signal validation (Tier 1 / Tier 2)
- `src/pynchy/ipc/_handlers_service.py` — Service request handler with policy enforcement

### The problem

The IPC channel is an intentional hole through container isolation. Currently it's wide open:
- `send_message`: container writes arbitrary text that gets sent verbatim to WhatsApp
- `schedule_task`: container specifies arbitrary prompt text for future agent runs
- `deploy`: container triggers process restart with arbitrary resume prompt
- `register_group`: container creates new groups with arbitrary config
- `create_periodic_agent`: container creates persistent agents with arbitrary persona

The authorization checks only verify source group, not content. A jailbroken agent in the admin group could craft malicious IPC payloads with no content validation.

### Design principle

**Containers can raise signals, not inject content.** The host derives behavior from which group sent the signal and from its own state, not from container-supplied payloads.

## Plan

### 1. Signal-only IPC (Tier 1)

Convert these IPC types to pure signals — no payload crosses the container boundary:

| Current type | Signal | Host behavior |
|-------------|--------|---------------|
| `send_message` (own group) | `message_ready` | Host reads agent's structured SDK output instead of trusting a separate file. For scheduled tasks, container runner captures SDK output and routes to chat. |
| `reset_context` | `context_reset` | Host clears session for the source group. It already knows which group from the folder path. |
| `refresh_groups` | `refresh_groups` | Host re-syncs metadata. No parameters needed. |

Signal file format: `{"signal": "<type>"}` — nothing else. The host identifies the source group from which directory the file appeared in.

#### send_message elimination

The most common IPC call. Two sub-cases:

- **Interactive conversations**: The Agent SDK output already flows back to the host via the container runner. The `send_message` MCP tool is redundant for the primary response — it's only used for mid-run progress updates. For progress updates, the host can read a `progress_ready` signal and pull the message from a host-controlled buffer (the SDK's streaming output).

- **Scheduled tasks**: Currently the only way to deliver results. Instead, the container runner should capture the task agent's SDK output and route it to the group's chat automatically. This eliminates the need for the agent to explicitly call `send_message`.

### 2. Deputy-mediated IPC (Tier 2)

These actions inherently carry data — you can't schedule a task without specifying what to do. The container writes the request with payload, but the host does NOT execute it directly. Instead, a Deputy agent reviews it first.

| Request type | Data that must cross | Deputy review |
|-------------|---------------------|---------------|
| `schedule_task` | Prompt text, schedule expression | Verify prompt isn't malicious, schedule is reasonable |
| `deploy` | Commit SHA, resume prompt | Verify commit exists, diff is safe |
| `register_group` | Group name, folder, trigger, config | Validate config, check for path traversal |
| `create_periodic_agent` | Name, schedule, CLAUDE.md content | Review agent persona and permissions |

Flow:
1. Container writes request file with payload to `/workspace/ipc/tasks/`
2. Host picks it up via inotify
3. Host spawns Deputy agent (in admin container) with the request as context
4. Deputy approves, rejects, or modifies the request
5. Host executes only if approved

Deputy latency is acceptable here — these are infrequent, high-impact operations. A few seconds of review is appropriate for actions like "create a new persistent agent."

### 3. Replace polling with inotify

Replace `asyncio.sleep(IPC_POLL_INTERVAL)` with filesystem event notifications using the `watchdog` library (cross-platform: inotify on Linux, FSEvents on macOS).

- **Startup**: Sweep directories for any files written while process was down (crash recovery — preserves the durability property of file-based IPC)
- **Running**: inotify fires on file creation, process immediately
- **Crash/reboot**: Files persist on disk, startup sweep picks them up

This eliminates the 1-second polling loop while keeping files as the durable message queue.

### 4. Shrink the file protocol

Currently IPC files contain rich JSON payloads. After this change:

**Tier 1 signals:**
```json
{"signal": "context_reset"}
```

**Tier 2 requests** (Deputy-mediated):
```json
{"signal": "schedule_task", "request_id": "uuid", "payload": { ... }}
```

The `request_id` allows the host to write a response file that the container polls for (existing pattern from `security-hardening-2-mcp-policy.md`). The `payload` is the data the Deputy reviews.

## Relationship to other security hardening steps

This is Step 0 — it hardens the transport before service integrations add new tools on top of it.

```
Step 0 (this): Narrow the IPC pipe (signal-only + Deputy mediation)
  ↓
Step 1: Security profiles define what each workspace can do (+ rate limits)
Step 2: Policy middleware evaluates requests against profiles (+ audit log)
  ↓
Step 6: Human approval gate for EXTERNAL-tier actions
  ↓
Steps 3-5: Service integrations (email, calendar, passwords) use the narrowed IPC
  ↓
Step 7: Input filtering for prompt injection (optional)
```

Steps 2 and 6 remain valuable even with a narrowed IPC surface — they handle the Tier 2 (Deputy-mediated) requests and any future MCP tools that need data-carrying IPC.

## Dependencies

- None for starting implementation. Can proceed in parallel with Steps 1-2.
- **Must complete before Steps 3-5.** Service integrations add new tools to the IPC surface; this step narrows that surface first. Without it, containers write arbitrary JSON payloads that the host trusts — the policy middleware (Step 2) gates execution, but the payload content itself isn't validated. Step 0's Deputy mediation for Tier 2 requests adds the validation layer needed before service adapters process those payloads.
- The `watchdog` library needs to be added as a dependency.

## Success criteria

- [ ] `send_message` eliminated for interactive conversations (host reads SDK output)
- [ ] `send_message` eliminated for scheduled tasks (container runner routes output to chat)
- [ ] `reset_context`, `refresh_groups` converted to pure signals
- [ ] `schedule_task`, `deploy`, `register_group`, `create_periodic_agent` routed through Deputy
- [x] Polling loop replaced with inotify/watchdog — DONE: `_watcher.py` uses `watchdog.Observer` with `FileSystemEventHandler`
- [x] Startup sweep processes files written while process was down — DONE: `_watcher.py._sweep_existing()` runs on startup
- [ ] Existing tests updated, new tests for signal processing and Deputy mediation
