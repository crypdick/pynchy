# Inter-Process Communication (IPC)

Containers are isolated from the host, so they communicate through a file-based IPC channel. The container writes JSON files to shared directories; the host polls those directories and processes them.

## Why File-Based

Containers have no network route back to the host. File mounts are the only shared surface, so IPC uses atomic file writes (temp file + rename) to safely pass structured messages between the two processes without sockets, HTTP, or message queues.

## Directory Layout

Each group gets its own IPC directory, mounted into the container at `/workspace/ipc`:

```
data/ipc/{group}/
├── messages/          # Container → host: outbound chat messages
├── tasks/             # Container → host: task/group management commands
├── input/             # Host → container: follow-up user messages
├── merge_results/     # Host → container: git sync responses
├── current_tasks.json # Host → container: read-only task snapshot
└── reset_prompt.json  # Host internal: context reset signal
```

## Message Flow (Container → Host)

1. Agent calls an MCP tool (e.g., `send_message`, `schedule_task`)
2. The MCP server (`ipc_mcp.py`, running inside the container) writes a JSON file atomically to the appropriate subdirectory
3. The host's IPC watcher (`ipc.py`) polls all group directories on a configurable interval
4. Host reads the file, authorizes the operation, executes it, and deletes the file
5. Failed files are moved to `data/ipc/errors/` for inspection

### Atomic writes

Both container and host use the same pattern to avoid partial reads:

```python
temp_path = filepath.with_suffix(".json.tmp")
temp_path.write_text(json.dumps(data))
temp_path.rename(filepath)          # atomic on same filesystem
```

The host only reads `.json` files, so the `.json.tmp` intermediate is never picked up.

## Message Flow (Host → Container)

When a user sends a follow-up message while the container is already running, the host writes to `data/ipc/{group}/input/`. The container's agent runner watches this directory and injects the message into the active conversation via stdin.

## IPC Message Types

### Messages (`messages/`)

Outbound chat messages. The agent can send messages mid-run without ending its turn.

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

`sender` is optional — used for multi-bot display in Telegram.

### Tasks (`tasks/`)

All other operations — scheduling, group management, deployment, git sync — go through the tasks directory. The `type` field determines the operation:

| Type | Purpose | God only? |
|------|---------|-----------|
| `schedule_task` | Create a recurring/one-time task | No (own group) |
| `schedule_host_job` | Schedule a shell command on the host | Yes |
| `pause_task` | Pause a task | No (own tasks) |
| `resume_task` | Resume a task | No (own tasks) |
| `cancel_task` | Delete a task | No (own tasks) |
| `register_group` | Register a new WhatsApp group | Yes |
| `refresh_groups` | Re-sync group metadata from WhatsApp | Yes |
| `create_periodic_agent` | Create a group + task + config for a periodic agent | Yes |
| `deploy` | Trigger a deployment (rebuild, restart) | Yes |
| `reset_context` | Clear session and chat history | No |
| `finished_work` | Signal that a scheduled task completed | No |
| `sync_worktree_to_main` | Merge worktree commits into main | No |

## Authorization

The host enforces permissions based on the source group's identity. See [Security Model](security.md#4-ipc-authorization) for the full authorization matrix.

## Container-Side MCP Server

The agent interacts with IPC through MCP tools exposed by `ipc_mcp.py` (runs as an MCP server inside the container). These tools validate inputs and write the appropriate JSON files. The agent never writes IPC files directly.

For the list of MCP tools available to agents, see [Scheduled Tasks](../usage/scheduled-tasks.md#mcp-tools-pynchy-server).
