# Scheduled Tasks

Schedule recurring or one-time tasks: briefings, maintenance scripts, periodic code reviews, or anything else that runs on a timer.

Three execution shapes share the scheduler:

- **Agent tasks** run the owning workspace's selected agent core, on the host
  or in a container according to its profile.
- **Deterministic workspace tasks** run a host command without an LLM and post
  its output through an owned workspace thread.
- **Host tasks** run infrastructure commands without a conversational
  workspace.

## Persistent Automation Memory

When Obsidian learning is enabled, every scheduled task owns a durable
directory at
`wiki/systems/pynchy/automation-memory/<task-id>/` in the configured vault.
Pynchy exposes that directory as `PYNCHY_AUTOMATION_MEMORY_DIR` to agent tasks,
pre-run gates, deterministic workspace commands, and host commands. Container
agents see `/home/agent/automation-memory`; host processes receive an absolute
host path.

Memory defaults on. Set `memory = false` in an automation's `[job]` table to
omit the directory, mount, and environment variable for that automation.
Disabling memory doesn't delete an existing task directory.

This memory belongs to the task ID, not its thread or provider session, so both
`continue` and `reset_before_run` preserve it. Pausing or removing a task leaves
its directory intact. Renaming a config-backed job creates a new task ID and
therefore a new memory directory.

All execution shapes use the canonical Obsidian directory directly. Pynchy
doesn't create or synchronize a runtime-owned mirror.

## Agent Tasks

Agent tasks run an agent on schedule. The agent gets a prompt and uses its
owning workspace's normal tools, skills, repo, admin status, and execution mode
as if a user had sent the message. A config-backed agent job names its policy
owner explicitly with `workspace`.

Pynchy derives one human-readable thread for each config-backed job. It names
the thread `<workspace> | <display_name>`, falling back to the config job name.
Every run finds or creates that thread, including reopening an archived one;
the persisted binding records the current JID while the logical workspace
continues to own policy. A semantic workspace can be
physically placed below a category while retaining its own profile. For
example, a `fam` job may create `fam | afternoon check-in` under
`#relationships`, but the thread runs and remains registered with only the
`fam` profile. Different jobs under the same workspace use different threads
and can run concurrently.

Temporal buffers one overlapping occurrence for a config-backed job. The next
run waits for the current one, then runs in the same task thread; Pynchy never
creates a numbered spillover thread for that job. This requires a channel with
child-thread support. Pynchy records an error instead of moving the run to
another target when the root channel cannot create threads.

Linear planning and execution tasks bind to the issue's routed conversation, so
every phase uses the issue thread's existing runtime. Pynchy refuses to run a
task whose destination cannot be bound.

Every scheduled task uses one of two session policies:

- `continue` resumes the thread's current provider session.
- `reset_before_run` clears the thread before each occurrence, posts `🗑️`, and
  starts a new durable provider session.

Configured agent jobs set `reset_before_run = true` by default. The reset is
visible even on the first occurrence. Temporal retries reuse the session created
for that occurrence and don't post another reset. Set the field to `false` when
successive occurrences should build on the same context.

A scheduled turn and ordinary messages share the thread's queue. A normal
message interrupts scheduled work after the current tool result, runs next in
the same session, and leaves the scheduled checkpoint available for Temporal to
resume. At most one worker owns the thread.

An agent completes a run by returning its final result. The worker process can
then stop, but the durable session remains resumable. Repo-backed agents can
publish with `sync_worktree_to_main`, which opens or updates a pull request for
committed changes. They resolve any error it returns and attach the PR to the
current Linear issue when one exists. Scheduled prompts don't need sentinel
commits.

During a scheduled run, the agent tries to resolve ordinary snags, bugs, and
tool failures itself. After the primary objective, the default post-work
reflection prompt asks it to review unresolved bugs, failures, and workflow
papercuts; search existing Linear and papercut records; and file only missing
reports. It doesn't report problems that it fixes during the run. The prompt
fragment lives at
`data/defaults/prompts/executors/post-work-reflection.md` and is injected into
scheduled agent work after the automation objective.

For a periodic review that turns evidence into approval-gated work proposals,
see [Schedule proactive proposals](../integrations/linear.md#schedule-proactive-proposals).

### Daily Triage Memo

A daily triage memo is a config-backed periodic agent that posts a short status
memo to its owned thread. Keep it read-only by prompt, reset its context before
each occurrence, and use a cheaper workspace model override:

```toml
# data/personalization/pynchy.toml
[profiles.admin]
is_admin = true

[workspaces.admin]
profiles = ["admin"]
model = "chatgpt/gpt-5.3-codex-spark"
```

```toml
# data/personalization/automations/daily-triage.toml
schema_version = 1

[job]
enabled = true
schedule = "0 8 * * *"
workspace = "admin"
reset_before_run = true
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

The example model name must exist in the active LiteLLM config. For Codex
workspaces backed by LiteLLM's ChatGPT subscription provider, keep the
`chatgpt/...` prefix.

One-time agent jobs use `at` instead of `schedule`:

```toml
# data/personalization/automations/cancel-youtube-premium.toml
schema_version = 1

[job]
enabled = true
at = "2026-07-08T18:30:00-07:00"
workspace = "admin"
prompt = """
Open a browser, log into YouTube, and cancel the YouTube Premium subscription.
"""
```

Use `interval_minutes` for config-backed interval jobs. An agent job can also
run a host-side gate before starting its agent:

```toml
# data/personalization/automations/marketplace-poller.toml
schema_version = 1

[job]
workspace = "marketplace-inbox-poller"
interval_minutes = 30
display_name = "marketplace inbox poller"
prompt = "Review the gate output and act on actionable messages."
pre_run_command = "scripts/marketplace_gate.py"
pre_run_cwd = "."
pre_run_timeout_seconds = 300
```

If the final non-empty line of successful gate output is JSON containing
`"wakeAgent": false`, Pynchy records a skipped run without starting an agent.
The job's durable thread binding still exists. Otherwise, stdout and stderr
become bounded pre-run context for the agent.

## Deterministic Workspace Tasks

Set `agent = false` for a script that does not need an LLM but still belongs to
a conversational workspace:

```toml
# data/personalization/automations/scheduler-watchdog.toml
schema_version = 1

[job]
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
`{"wakeAgent": false}` skips delivery but retains the job's thread binding.
Pynchy creates or repairs active and paused task bindings at startup, before
their first run. Discord forum workspaces tag these posts as `automation`.

## Plugin-Sourced Jobs

Plugins can implement `pynchy_job_specs` to load jobs from another durable
registry. Contributions use the same `JobConfig` fields and enter the same
database and Temporal reconciliation paths as file-backed automations.
Personalized config wins on name collisions. A plugin should store logical
`workspace` owners, never chat JIDs or generated thread folder names.

Host jobs use the reserved workspace name `host`:

```toml
# data/personalization/automations/backup-runtime-dbs.toml
schema_version = 1

[job]
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

Temporal fires the workflows. Each workflow runs an activity in the Pynchy host
process, so agent containers, IPC streaming, shell execution, task logs, and
worktree isolation stay on the existing host runner path.

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
reconcile_schedules = true
git_sync_interval_seconds = 300
channel_reconciliation_interval_seconds = 300
auto_deploy = false
```

When `auto_deploy` stays `false` (the default), Pynchy detects a newer repository
revision without changing the local checkout. It posts an update prompt to the
configured admin workspace. Use the channel's **Fetch and upgrade** action to fetch
the revision and deploy it. Channels without interactive controls direct the operator
to run `uv run pynchy deploy` on the host. Set `auto_deploy = true` to pull and
deploy eligible source changes automatically.

Pynchy requires a reachable Temporal service when the scheduler starts. It does not fall back to local due-work execution. The local scheduler loop only reconciles desired state from config and SQLite into Temporal; it does not decide that a task is due or run shell commands itself.

Set `reconcile_schedules = false` only for a shadow migration instance. Its
Temporal worker remains available for explicit test workflows, but Pynchy does
not create recurring schedules or delayed workflows from configuration and
SQLite. Enable reconciliation on exactly one authoritative instance.

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

The script backs up `messages.db`, `neonize.db`, and `temporal.db` into
`data/backups` by default and prunes backups older than 30 days. Set
`PYNCHY_BACKUP_KEEP_COUNT` to a positive integer to also cap the number of
retained generations; `0` leaves the count uncapped. The launchd template
keeps the newest seven generations. It briefly unloads the
`com.pynchy.temporal` LaunchAgent while snapshotting `temporal.db`, then loads
it again. This prevents the online SQLite backup from blocking Temporal writes
and leaves other Pynchy components running. Set `PYNCHY_TEMPORAL_LABEL` and
`PYNCHY_TEMPORAL_PLIST` when the deployment uses different launchd
identifiers.

For host-loss protection, set both `PYNCHY_BACKUP_REMOTE_HOST` and `PYNCHY_BACKUP_REMOTE_DIR`. The script creates SQLite snapshots in `PYNCHY_BACKUP_STAGING_DIR`, transfers them with `rsync`, verifies `SHA256SUMS` on the remote host, and only then renames the hidden partial directory to its final timestamp. Set `PYNCHY_BACKUP_SSH_KEY` when the scheduled job needs a dedicated noninteractive key. Remote hosts must provide `bash`, `rsync`, and `sha256sum`:

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

Host tasks run shell commands on the host — no LLM, no container. Use declared automation files for maintenance scripts, backups, git operations, or anything that doesn't need an agent.

### Automation file

Static host jobs belong in `data/personalization/automations/`. They are useful
for always-on maintenance jobs that are part of the deployment.

```toml
# data/personalization/automations/backup-db.toml
schema_version = 1

[job]
enabled = true
workspace = "host"
schedule = "0 3 * * *"          # daily at 3am
command = "scripts/backup.sh"
cwd = "."                       # relative to project root (optional)
timeout_seconds = 600           # default: 600
quiet_on_success = true         # suppress clean-run output logging
```

Config host jobs use `workspace = "host"` and currently support cron
expressions. Pynchy reconciles enabled config host jobs into Temporal Schedules,
and Temporal triggers host-process activities for each run. They don't show up
in `list_tasks` (static config, not database entries).

Pynchy validates and applies automation-file changes without restarting. An
add, update, disable, removal, workspace reassignment, or referenced prompt
change updates the configured task rows and Temporal schedules together. An
invalid edit or failed reconciliation leaves the previous runtime snapshot
active and retries during the next configuration poll.

## MCP Tools

`list_tasks` reports the durable configured and routed work visible to the caller without revealing task prompts. Create recurring work by committing an automation file instead of asking an agent to create a database row. For a lasting change to an automation-backed task, update the automation file or the owning Linear work item; those sources reconcile their task state.

| Tool | Purpose |
|------|---------|
| `list_tasks` | Show visible configured and routed work |
| `get_scheduled_task` | Read one visible persisted task's prompt and editable metadata |
| `update_scheduled_task` | Update a visible persisted task's prompt or active/paused status |
| `pause_task` | Pause a visible task projection |
| `resume_task` | Resume a visible task projection |
| `cancel_task` | Cancel a visible task projection |
| `send_message` | Send a message to the group (agent tasks only) |
| `list_todos` | List pending todo items (or all items with `include_done: true`) |
| `complete_todo` | Mark a todo item as done by ID |

`get_scheduled_task` and `update_scheduled_task` expose only agent tasks. A
non-admin workspace can access only tasks it owns; an admin can access agent
tasks across workspaces. Missing and unauthorized task IDs return the same
not-found result. Host jobs stay outside this surface because their commands
and working directories need host-level authority.

The update tool accepts only a non-empty prompt and `active` or `paused`
status. It preserves schedule, task ID, workspace binding, and task-owned
automation memory. Resuming uses the normal task resume path, preserving
one-shot and failure-window behavior. Automation-backed tasks reject direct
updates because their automation definition remains the source of truth.

## Schedule Types

Config-backed agent and deterministic workspace jobs support these schedule types:

| Type | Value Format | Example |
|------|--------------|---------|
| `cron` | Cron expression | `0 9 * * 1` (Mondays at 9am) |
| `interval` | Milliseconds | `3600000` (every hour) |
| `once` | ISO timestamp | `2024-12-25T09:00:00Z` |

Config-backed agent and deterministic workspace jobs use `schedule` for cron,
`interval_minutes` for intervals, or `at` for one-time execution. Config-file
host jobs only support cron.

`cron` and `interval` entries run as Temporal Schedules. `once` entries run as delayed Temporal workflows. Temporal owns the wake-up; Pynchy activities own the actual agent or host-shell execution.
