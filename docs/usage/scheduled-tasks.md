# Scheduled Tasks

Schedule recurring or one-time tasks: briefings, maintenance scripts, periodic code reviews, or anything else that runs on a timer.

Three execution shapes share the native scheduler:

- **Agent tasks** run the owning workspace's selected agent core, on the host
  or in a container according to its profile.
- **Deterministic workspace tasks** run a host command without an LLM and post
  its output through an owned workspace thread.
- **Host tasks** run infrastructure commands without a conversational
  workspace.

## Agent Tasks

Agent tasks run an agent on schedule. The agent gets a prompt and uses its
owning workspace's normal tools, skills, repo, admin status, and execution mode
as if a user had sent the message. A config-backed agent job names its policy
owner explicitly with `workspace`.

Pynchy derives one human-readable thread for each config-backed job. It names
the thread `<workspace> | <display_name>`, falling back to the config job name.
Every run finds or creates that thread, including reopening an archived one;
it never stores a Discord thread JID as policy. A semantic workspace can be
physically placed below a category while retaining its own profile. For
example, a `fam` job may create `fam | afternoon check-in` under
`#relationships`, but the thread runs and remains registered with only the
`fam` profile. Different jobs under the same workspace use different threads
and can run concurrently.

Temporal buffers one overlapping occurrence for a config-backed job. The next run waits for the current one, then runs in the same task thread; Pynchy never creates a numbered spillover thread for that job. This requires a channel with child-thread support. Pynchy records an error instead of moving the run to another target when the root channel cannot create threads.

Tasks created through `schedule_task` continue to target their selected chat directly. When that chat is busy, they retain the existing numbered-child-thread behavior. Each agent task uses an isolated runtime folder. If its workspace profile selects a repo, worktree commits merge and push after a successful run.

For a periodic review that turns evidence into approval-gated work proposals,
see [Schedule proactive proposals](../integrations/linear.md#schedule-proactive-proposals).

### Daily Triage Memo

A daily triage memo is a config-backed periodic agent that posts a short status memo to an explicit Pynchy channel. Keep it read-only by prompt and use an isolated context plus a cheaper workspace model override:

```toml
[profiles.admin]
is_admin = true

[workspaces.admin]
profiles = ["admin"]
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

Use `interval_minutes` for config-backed interval jobs. An agent job can also
run a host-side gate before creating its thread:

```toml
[jobs.marketplace-poller]
workspace = "marketplace-inbox-poller"
interval_minutes = 30
display_name = "marketplace inbox poller"
prompt = "Review the gate output and act on actionable messages."
pre_run_command = "scripts/marketplace_gate.py"
pre_run_cwd = "."
pre_run_timeout_seconds = 300
```

If the final non-empty line of successful gate output is JSON containing
`"wakeAgent": false`, Pynchy records a skipped run without creating a thread or
starting an agent. Otherwise, stdout and stderr become bounded pre-run context
for the agent.

## Deterministic Workspace Tasks

Set `agent = false` for a script that does not need an LLM but still belongs to
a conversational workspace:

```toml
[jobs.scheduler-watchdog]
workspace = "cron"
schedule = "0 23 * * *"
display_name = "scheduler health watchdog"
agent = false
command = "scripts/scheduler_watchdog.py"
cwd = "."
timeout_seconds = 300
```

The command runs on the host. Non-empty output goes to the derived thread under
the workspace's physical Discord root, and Pynchy registers that thread with
the logical owner's profile for future replies. Successful output ending in
`{"wakeAgent": false}` skips delivery and does not create a thread.

## Plugin-Sourced Jobs

Plugins can implement `pynchy_job_specs` to load jobs from another durable
registry. Contributions use the same `JobConfig` fields and enter the same
database and Temporal reconciliation paths as `[jobs.*]`. User config wins on
name collisions. A plugin should store logical `workspace` owners, never chat
JIDs or generated thread folder names.

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

Host commands are not retried within the same Temporal occurrence. A command
may have changed external state before failing or losing its worker, so its next
scheduled occurrence is the retry boundary.

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
auto_deploy = false
```

When `auto_deploy` stays `false` (the default), Pynchy detects a newer repository
revision without changing the local checkout. It posts an update prompt to the
configured admin workspace. Use the channel's **Fetch and upgrade** action to fetch
the revision and deploy it. Channels without interactive controls direct the operator
to the local control-plane `POST /deploy` endpoint. Set `auto_deploy = true` to pull
and deploy eligible source changes automatically.

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

The script backs up `messages.db`, `memories.db`, `neonize.db`, and `temporal.db` into `data/backups` by default and prunes backups older than 30 days. It briefly unloads the `com.pynchy.temporal` LaunchAgent while snapshotting `temporal.db`, then loads it again. This prevents the online SQLite backup from blocking Temporal writes and leaves other Pynchy components running. Set `PYNCHY_TEMPORAL_LABEL` and `PYNCHY_TEMPORAL_PLIST` when the deployment uses different launchd identifiers.

For host-loss protection, set both `PYNCHY_BACKUP_REMOTE_HOST` and `PYNCHY_BACKUP_REMOTE_DIR`. The script creates SQLite snapshots in `PYNCHY_BACKUP_STAGING_DIR`, transfers them with `rsync`, verifies `SHA256SUMS` on the remote host, and only then renames the hidden partial directory to its final timestamp. Set `PYNCHY_BACKUP_SSH_KEY` when the scheduled job needs a dedicated noninteractive key. Remote hosts must provide `ssh`, `rsync`, and `sha256sum`:

```bash
PYNCHY_BACKUP_REMOTE_HOST=backup@example-nas \
PYNCHY_BACKUP_REMOTE_DIR=/srv/backups/pynchy-runtime-dbs \
PYNCHY_BACKUP_SSH_KEY="$HOME/.ssh/pynchy_backup_ed25519" \
scripts/backup_runtime_dbs.sh
```

To run a remote backup daily on macOS:

```bash
cp launchd/com.pynchy.backup.plist ~/Library/LaunchAgents/com.pynchy.backup.plist
```

Before loading the plist, replace `$HOME` and `$PYNCHY_PROJECT_ROOT` with absolute paths, and replace `$PYNCHY_BACKUP_REMOTE_HOST` and `$PYNCHY_BACKUP_REMOTE_DIR` with the remote SSH host and absolute destination. Ensure the host key is already trusted and the configured key works with `BatchMode=yes`.

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

Config-backed agent and deterministic workspace jobs use `schedule` for cron,
`interval_minutes` for intervals, or `at` for one-time execution. Config-file
host jobs only support cron.

`cron` and `interval` entries run as Temporal Schedules. `once` entries run as delayed Temporal workflows. Temporal owns the wake-up; Pynchy activities own the actual agent or host-shell execution.
