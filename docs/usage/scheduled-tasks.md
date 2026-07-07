# Scheduled Tasks

Schedule recurring or one-time tasks: briefings, maintenance scripts, periodic code reviews, or anything else that runs on a timer.

Two kinds: **agent tasks** (run a Claude agent in a container) and **host tasks** (run shell commands on the host). Both use the same MCP tools.

## Agent Tasks

Agent tasks spin up a containerized Claude agent on schedule. The agent gets a prompt and uses its normal tools (Bash, MCP, etc.), as if a user had sent the message. Any group can schedule agent tasks for itself; the admin group can schedule them for any group.

### Context Modes

| Mode | Behavior |
|------|----------|
| `group` | Runs in the group's current session (shares conversation history) |
| `isolated` | Runs in a fresh session each time |

Agent tasks can optionally send messages to their group via `send_message`, or finish silently. Each run is logged to the database with duration and result. If the task has `pynchy_repo_access`, worktree commits merge and push after a successful run.

## Temporal Scheduler

Pynchy starts each due agent task as a Temporal workflow. The workflow runs an activity in the Pynchy host process, so agent containers, IPC streaming, task logs, and worktree merge behavior stay on the existing host runner path.

```toml
[scheduler]
temporal_address = "localhost:7233"
temporal_namespace = "default"
temporal_task_queue = "pynchy-scheduler"
```

Pynchy requires a reachable Temporal service when the scheduler starts. It does not fall back to local scheduled-agent execution. Host tasks still run through the local scheduler.

The `/status` endpoint includes a `temporal` section:

| Field | Meaning |
|-------|---------|
| `address` | Configured Temporal server address |
| `namespace` | Configured Temporal namespace |
| `task_queue` | Task queue used by the Pynchy scheduler worker |
| `cluster_healthy` | Result of the Temporal WorkflowService health check (`true`, `false`, or `null` if unreachable) |
| `worker_running` | Whether this Pynchy process has an active Temporal worker |
| `last_workflow_id` | Most recent scheduled-agent workflow started or handled by this process |
| `last_task_id` | Scheduled task ID for the most recent workflow event |
| `last_result` | `started`, `already_started`, `completed`, `skipped`, or `error` |
| `last_error` | Last scheduler dispatch or activity error, if any |

### Single-host macOS service

For a personal macOS deployment, run a local Temporal service bound to loopback with a persisted SQLite database:

```bash
brew install temporal
mkdir -p ~/Library/Logs/pynchy data
cp launchd/com.pynchy.temporal.plist ~/Library/LaunchAgents/com.pynchy.temporal.plist
```

Before loading the plist, replace `$HOME` in `~/Library/LaunchAgents/com.pynchy.temporal.plist` with your absolute home directory. `launchd` does not expand shell variables inside plist string values.

```bash
plutil -lint ~/Library/LaunchAgents/com.pynchy.temporal.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.pynchy.temporal.plist
launchctl kickstart "gui/$(id -u)/com.pynchy.temporal"
```

Useful checks:

```bash
launchctl print "gui/$(id -u)/com.pynchy.temporal"
temporal operator cluster health --address 127.0.0.1:7233
lsof -nP -iTCP:7233 -sTCP:LISTEN
tail -n 100 ~/Library/Logs/pynchy/temporal.err.log
```

The local service stores its durable state at `data/temporal.db`. Back up this file with the rest of `data/`; losing it drops Temporal workflow history and idempotency state for scheduled agent tasks. This single-host setup is suitable for a personal Mac deployment. Use Temporal Cloud or a normal self-hosted Temporal cluster when the scheduler needs HA or multi-host durability.

## Host Tasks

Host tasks run shell commands on the host — no LLM, no container. Use them for maintenance scripts, backups, git operations, or anything that doesn't need an agent. Only the admin group can create and manage host tasks.

Two ways to define them:

### Config file (`config.toml`)

Static cron jobs defined in config. Good for always-on maintenance jobs that are part of the deployment.

```toml
[cron_jobs.backup_db]
schedule = "0 3 * * *"          # daily at 3am
command = "scripts/backup.sh"
cwd = "."                       # relative to project root (optional)
timeout_seconds = 600           # default: 600
enabled = true                  # default: true
```

Config cron jobs only support cron expressions. The scheduler polls them each tick and runs them in the host process. They don't show up in `list_tasks` (static config, not database entries).

### MCP tool (`schedule_task` with `task_type: "host"`)

Agents in the admin group can create host jobs dynamically via `schedule_task` with `task_type` set to `"host"`. The database stores them, and they support all schedule types (cron, interval, once). They show up in `list_tasks` and can be paused/resumed/cancelled like agent tasks.

## MCP Tools

One set of tools manages all task types. `schedule_task` takes a `task_type` parameter (`"agent"` or `"host"`) to pick what kind of task to create. Management tools (`list_tasks`, `pause_task`, etc.) work on both — host job IDs carry a `host-` prefix so routing happens automatically.

| Tool | Purpose |
|------|---------|
| `schedule_task` | Schedule an agent task or host job (`task_type` field) |
| `list_tasks` | Show all tasks — agent and host — with `[agent]`/`[host]` labels |
| `pause_task` | Pause a task (any type) |
| `resume_task` | Resume a paused task (any type) |
| `cancel_task` | Delete a task (any type) |
| `send_message` | Send a message to the group (agent tasks only) |
| `list_todos` | List pending todo items (or all items with `include_done: true`) |
| `complete_todo` | Mark a todo item as done by ID |

## Schedule Types

Both agent tasks and database host jobs support these schedule types:

| Type | Value Format | Example |
|------|--------------|---------|
| `cron` | Cron expression | `0 9 * * 1` (Mondays at 9am) |
| `interval` | Milliseconds | `3600000` (every hour) |
| `once` | ISO timestamp | `2024-12-25T09:00:00Z` |

Config-file host cron jobs only support `cron`.
