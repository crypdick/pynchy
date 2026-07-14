# Scheduled Tasks

Schedule recurring or one-time tasks: briefings, maintenance scripts, periodic code reviews, or anything else that runs on a timer.

Two kinds: **agent tasks** (run a Claude agent in a container) and **host tasks** (run shell commands on the host). Both use the same MCP tools.

## Agent Tasks

Agent tasks spin up a containerized agent on schedule. The agent gets a prompt and uses its normal tools (Bash, MCP, etc.), as if a user had sent the message. Config-backed jobs target a named workspace.

Agent tasks always run in a dedicated isolated thread for the target workspace. They can optionally send messages to their group via `send_message`, or finish silently. Each run is logged to the database with duration and result. If the workspace profile selects a repo, worktree commits merge and push after a successful run.

### Daily Triage Memo

A daily triage memo is a config-backed periodic agent that posts a short status memo to an explicit Pynchy channel. Keep it read-only by prompt and use an isolated context plus a cheaper workspace model override:

```toml
[profiles.pynchy-admin]
is_admin = true

[workspaces.admin]
profiles = ["pynchy-admin"]
model = "chatgpt/gpt-5.3-codex-spark"

[jobs.daily-triage]
enabled = true
schedule = "0 8 * * *"
workspace = "admin"
prompt = """
Produce the daily Pynchy triage memo.

Review recent scheduled task health, failed runs, Temporal scheduler status,
stale PR/branch/CI signals if available, and recent Pynchy/operator notes.
Keep the run read-only except for writing a dated memo/report note if useful.
Do not edit config, cron jobs, branches, PRs, or external systems.

Send a concise memo to this Pynchy channel every run:
- Top 3 findings or "no urgent findings".
- Any failing or paused scheduled work.
- Suggested next actions with concrete repo paths, URLs, or commands when useful.
- Links/paths to any full report you wrote.
"""
```

Replace the `chat` value with the real Pynchy channel/topic ref for the deployment. The example model name must exist in the active LiteLLM config; for Codex workspaces backed by LiteLLM's ChatGPT subscription provider, keep the `chatgpt/...` prefix.

One-time agent jobs use `at` instead of `schedule`:

```toml
[jobs.cancel-youtube-premium]
enabled = true
at = "2026-07-08T18:30:00-07:00"
workspace = "admin"
prompt = """
Open a browser, log into YouTube, and cancel the YouTube Premium subscription.
"""
```

Host jobs use the reserved workspace name `host`:

```toml
[jobs.backup-runtime-dbs]
enabled = true
schedule = "0 3 * * *"
workspace = "host"
command = "scripts/backup_runtime_dbs.sh"
cwd = "."
timeout_seconds = 600
quiet_on_success = true
```

## Temporal Scheduler

Pynchy reconciles scheduled work into Temporal. Recurring agent tasks, database host jobs, and config-file host cron jobs become Temporal Schedules. One-time agent tasks and one-time host jobs become delayed Temporal workflows.

Temporal fires the workflows. Each workflow runs an activity in the Pynchy host process, so agent containers, IPC streaming, shell execution, task logs, and worktree merge behavior stay on the existing host runner path.

Long-running agent activities heartbeat while they run. If the host restarts, Pynchy uses the
[durable interrupted-turn checkpoint](../architecture/message-routing.md#interrupted-turn-recovery)
to continue an unfinished scheduled agent in its existing conversation instead of starting the
task prompt again.

```toml
[scheduler]
temporal_address = "localhost:7233"
temporal_namespace = "default"
temporal_task_queue = "pynchy-scheduler"
git_sync_interval_seconds = 300
channel_reconciliation_interval_seconds = 300
```

Pynchy requires a reachable Temporal service when the scheduler starts. It does not fall back to local due-work execution. The local scheduler loop only reconciles desired state from config and SQLite into Temporal; it does not decide that a task is due or run shell commands itself.

The `/status` endpoint includes a `temporal` section:

| Field | Meaning |
|-------|---------|
| `address` | Configured Temporal server address |
| `namespace` | Configured Temporal namespace |
| `task_queue` | Task queue used by the Pynchy scheduler worker |
| `cluster_healthy` | Result of the Temporal WorkflowService health check (`true`, `false`, or `null` if unreachable) |
| `worker_running` | Whether this Pynchy process has an active Temporal worker |
| `last_workflow_id` | Most recent scheduled-work workflow started or handled by this process |
| `last_task_id` | Scheduled task or host job ID for the most recent workflow event |
| `last_result` | `started`, `already_started`, `completed`, `skipped`, or `error` |
| `last_error` | Last scheduler dispatch or activity error, if any |

### Single-host macOS service

For a personal macOS deployment, run a local Temporal service bound to loopback with a persisted SQLite database:

```bash
brew install temporal
mkdir -p ~/Library/Logs/pynchy data
cp launchd/com.pynchy.temporal.plist ~/Library/LaunchAgents/com.pynchy.temporal.plist
```

Before loading the plist, replace `$HOME` with your absolute home directory and `$PYNCHY_PROJECT_ROOT` with the absolute path to this checkout. `launchd` does not expand shell variables inside plist string values.

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

The local service stores its durable state at `data/temporal.db`. Back up this file with the rest of `data/`; losing it drops Temporal workflow history, schedule state, delayed starts, and idempotency state for scheduled work. This single-host setup is suitable for a personal Mac deployment. Use Temporal Cloud or a normal self-hosted Temporal cluster when the scheduler needs HA or multi-host durability.

To back up runtime databases with SQLite-safe snapshots, run:

```bash
scripts/backup_runtime_dbs.sh
```

The script backs up `messages.db`, `memories.db`, `neonize.db`, and `temporal.db` into `~/Library/Mobile Documents/com~apple~CloudDocs/PynchyBackups` by default and prunes backups older than 30 days. To run it daily on macOS:

```bash
cp launchd/com.pynchy.backup.plist ~/Library/LaunchAgents/com.pynchy.backup.plist
```

Before loading the plist, replace `$HOME` with your absolute home directory and `$PYNCHY_PROJECT_ROOT` with the absolute path to this checkout.

```bash
plutil -lint ~/Library/LaunchAgents/com.pynchy.backup.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.pynchy.backup.plist
launchctl kickstart "gui/$(id -u)/com.pynchy.backup"
```

## Host Tasks

Host tasks run shell commands on the host — no LLM, no container. Use them for maintenance scripts, backups, git operations, or anything that doesn't need an agent. Only the admin group can create and manage host tasks.

Two ways to define them:

### Config file (`config.toml`)

Static host jobs defined in config. Good for always-on maintenance jobs that are part of the deployment.

```toml
[jobs.backup_db]
enabled = true
workspace = "host"
schedule = "0 3 * * *"          # daily at 3am
command = "scripts/backup.sh"
cwd = "."                       # relative to project root (optional)
timeout_seconds = 600           # default: 600
quiet_on_success = true         # suppress clean-run output logging
```

Config host jobs use `workspace = "host"` and currently support cron expressions. Pynchy reconciles enabled config host jobs into Temporal Schedules, and Temporal triggers host-process activities for each run. They don't show up in `list_tasks` (static config, not database entries).

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

`cron` and `interval` entries run as Temporal Schedules. `once` entries run as delayed Temporal workflows. Temporal owns the wake-up; Pynchy activities own the actual agent or host-shell execution.
