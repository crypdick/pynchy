# Inter-Process Communication (IPC)

Containers talk to the host through a file-based IPC channel. The container writes JSON files to shared directories; the host watches for filesystem events (watchdog/inotify) and processes them immediately.

## Why File-Based

Containers have no network route back to the host. File mounts are the only shared surface, so IPC uses atomic file writes (temp file + rename) to pass structured messages between the two processes — no sockets, HTTP, or message queues required.

## Directory Layout

Each group gets its own IPC directory, mounted into the container at `/run/pynchy`:

```
data/ipc/{group}/
├── messages/          # Container → host: outbound chat messages
├── requests/          # Container → host: request envelopes
├── responses/         # Host → container: service request responses
├── input/             # Host → container: follow-up user messages
├── merge_results/     # Host → container: git publication responses
├── current_tasks.json # Host → container: read-only task snapshot
├── todos.json         # Shared: host writes, container reads/manages
└── reset_prompt.json  # Host internal: context reset signal
```

## Message Flow (Container → Host)

1. Agent calls an MCP tool (e.g., `send_message`, `register_group`)
2. The MCP server (running inside the container) writes a JSON file atomically to the appropriate subdirectory
3. The host's IPC watcher (`ipc/_watcher.py`) detects the new file via watchdog (inotify on Linux, FSEvents on macOS)
4. Host reads the file, authorizes the operation, executes it, and deletes the file
5. Failed files are moved to `data/ipc/errors/` for inspection
6. On startup, the watcher sweeps all directories for files written while the process was down (crash recovery)

### Atomic writes

Writers publish complete JSON with a same-directory rename, so watchers never
read partial files. Host writes into container-visible directories use an
unpredictable, exclusively created temporary name. The host opens that directory
without following its final symlink component and performs the replacement
relative to the open directory. A container-created temporary-file symlink
therefore cannot redirect a privileged host write.

## Message Flow (Host → Container)

When a user sends a follow-up message while the container is running, the host writes to `data/ipc/{group}/input/`. The container's agent runner watches this directory and injects the message into the active conversation via stdin.

## IPC Protocol

IPC files use one of two formats depending on their tier:

### Tier 1: Signals

Signals carry no payload. The host derives behavior from the signal type and the sending group.

```json
{"signal": "refresh_groups"}
```

| Signal | Purpose | God only? |
|--------|---------|-----------|
| `refresh_groups` | Re-sync group metadata | Yes |

### Tier 2: Data-carrying requests

Request envelopes use `kind` to select an operation and `payload` to carry
handler-owned data. Outbound chat messages use a separate `type` field.

#### Messages (`messages/`)

Outbound chat messages. The agent sends messages mid-run without ending its turn.

```json
{
  "type": "message",
  "chatJid": "123@g.us",
  "text": "Working on it...",
  "groupFolder": "my-group",
  "timestamp": "2025-01-15T10:30:00Z",
  "sender": "Researcher"
}
```

`sender` — optional assistant display name for channels that support it.

#### Requests (`requests/`)

All other operations — scheduling, group management, deployment, git sync — go through the requests directory. Every request file uses the same versioned envelope; the `kind` field determines the operation and `payload` carries handler-owned data:

```json
{
  "schema_version": 1,
  "kind": "register_group",
  "request_id": "uuid-...",
  "source_group": "my-group",
  "created_at": "2026-07-07T12:00:00+00:00",
  "reply_to": null,
  "deadline": null,
  "payload": {
    "prompt": "Send a daily briefing",
    "schedule_type": "cron",
    "schedule_value": "0 9 * * *",
    "targetGroup": "my-group"
  }
}
```

Host-mutating request kinds are claimed in `request_ledger/{request_id}.json` before execution so a replayed file cannot perform the mutation twice.
The host accepts `request_id` only as one printable path component of at most
128 UTF-8 bytes. Path separators, `.` and `..` cannot select a response path
outside the group's `responses/` directory.

| Kind | Purpose | God only? |
|------|---------|-----------|
| `schedule_host_job` | Schedule a shell command on the host | Yes |
| `automation_status` | List config-backed automation definitions | No (own workspace) |
| `automation_definition` | Read one automation definition | No (own workspace) |
| `create_automation` | Create an automation config definition | Yes |
| `update_automation` | Update an automation config definition | Yes |
| `pause_automation` | Disable an automation definition | Yes |
| `resume_automation` | Enable an automation definition | Yes |
| `delete_automation` | Delete an automation definition | Yes |
| `register_group` | Register a new chat group | Yes |
| `messaging_source_health` | Read body-free source readiness and persisted ingress freshness | No |
| `deploy` | Trigger a deployment (rebuild, restart) | Yes |
| `reset_context` | Clear session and chat history | No |
| `sync_worktree_to_main` | Push worktree commits and open or update a PR | No |
| `rebase_managed_feature` | Rebase one managed feature onto its remote default branch | No |
| `publish_managed_feature` | Open or update a PR for one managed feature | No |

#### Managed feature PR publication

The agent-facing `publish_managed_feature` tool accepts only a canonical
`feature_slug`. The tool fixes publication to a pull request. It does not
accept a repository, worktree path, branch, target branch, merge mode, or
deployment mode.

The host resolves the slug from an active version-2 `.new-feature/manifest.toml`
under one configured repository root and writes the publication result to
`merge_results/<request_id>.json`. It rejects missing or ambiguous manifest
records; it never scans or falls back across worktree directories. See the
[security model](security.md#5c-host-mutating-operations-cop-gate) for the
manifest binding, approval receipt, and pre-push check.

`rebase_managed_feature` accepts the same canonical slug and derives the same
manifest-bound repository, worktree, branch, and remote default branch. It
rejects dirty or invalid worktrees, rebases only onto the host-verified remote
SHA, and never pushes, opens a pull request, merges, or deploys. If it leaves a
conflict, the agent resolves it with `git rebase --continue`, `--abort`, or
`--skip` before publishing.

## Authorization

The host enforces permissions based on the source group's identity. See [Security Model](security.md#4-ipc-authorization) for the full authorization matrix.

## Service Requests

Service requests use the `service:<tool_name>` kind prefix for request-response IPC. The container writes a request with a unique `request_id` to `requests/`, and the host writes the response to `responses/{request_id}.json`. The container watches for the response file.

Service requests pass through the [security policy middleware](security.md) before dispatch. Plugin-provided handlers process the request and return a result or error.

Current service tools:

- **Calendar** — `list_calendars`, `list_calendar`, `create_event`, `delete_event` (CalDAV plugin)

## Security Requests

Security requests use the `security:` type prefix. Unlike service requests (initiated by MCP tools), security requests originate from the agent runner's `BEFORE_TOOL_USE` hooks — the agent never sees them unless a command is blocked.

### `security:bash_check`

The container's bash security hook sends this request when a command is not on the local whitelist (i.e., it is network-capable or unknown). The host evaluates the command against the session's taint state and returns a decision.

**Request** (container writes to `requests/`):
```json
{
  "schema_version": 1,
  "kind": "security:bash_check",
  "request_id": "uuid-...",
  "source_group": "my-group",
  "created_at": "2026-07-07T12:00:00+00:00",
  "reply_to": "responses",
  "deadline": null,
  "payload": {
    "command": "curl https://example.com/api"
  }
}
```

**Response** (host writes to `responses/{request_id}.json`):
```json
{"decision": "allow"}
```

```json
{"decision": "deny", "reason": "Cop flagged command as potential exfiltration"}
```

When the decision is `needs_human`, the host creates a pending approval (broadcast to the chat channel) and does **not** write a response file. The container blocks until the human approves or denies, or the 300-second timeout expires.

The `security:` prefix is registered as a prefix handler — all `security:*` IPC types route to the same handler module, so adding new security gates needs no extra IPC wiring.

## Container-Side MCP Server

The agent interacts with IPC through MCP tools exposed by the agent tools MCP server (running inside the container). These tools validate inputs and write the JSON files. The agent never writes IPC files directly.

For the list of MCP tools available to agents, see [Scheduled Tasks](../usage/scheduled-tasks.md#mcp-tools).
