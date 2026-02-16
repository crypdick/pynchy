# Scheduled Tasks

Pynchy supports two kinds of scheduled tasks: **agent tasks** that run a full Claude agent inside a container, and **host tasks** that execute shell commands directly on the host machine. Both are managed through the same set of MCP tools.

## Agent Tasks

Agent tasks spin up a containerized Claude agent on schedule. The agent receives a prompt and has access to all its normal tools (Bash, MCP, etc.), just as if a user had sent a message. Any group can schedule agent tasks for itself; the god group can schedule them for any group.

### Context Modes

| Mode | Behavior |
|------|----------|
| `group` | Runs in the group's current session (shares conversation history) |
| `isolated` | Runs in a fresh session each time |

Agent tasks can optionally send messages to their group via `send_message`, or complete silently. Task runs are logged to the database with duration and result. If the task has `project_access`, worktree commits are merged and pushed after a successful run.

## Host Tasks

Host tasks run shell commands directly on the host — no LLM, no container. Use them for maintenance scripts, backups, git operations, or anything that doesn't need an agent. Only the god group can create and manage host tasks.

There are two ways to define them:

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

Config cron jobs only support cron expressions. They are polled each scheduler tick and run in the host process. They are not visible in `list_tasks` (they're static config, not database entries).

### MCP tool (`schedule_task` with `task_type: "host"`)

Agents in the god group can create host jobs dynamically via `schedule_task` with `task_type` set to `"host"`. These are stored in the database and support all schedule types (cron, interval, once). They appear in `list_tasks` and can be paused/resumed/cancelled like agent tasks.

## MCP Tools

All task types are managed through a single set of tools. The `schedule_task` tool uses a `task_type` parameter (`"agent"` or `"host"`) to determine what kind of task to create. The management tools (`list_tasks`, `pause_task`, etc.) work on both types — host job IDs are prefixed with `host-` so routing is automatic.

| Tool | Purpose |
|------|---------|
| `schedule_task` | Schedule an agent task or host job (`task_type` field) |
| `list_tasks` | Show all tasks — agent and host — with `[agent]`/`[host]` labels |
| `pause_task` | Pause a task (any type) |
| `resume_task` | Resume a paused task (any type) |
| `cancel_task` | Delete a task (any type) |
| `send_message` | Send a message to the group (agent tasks only) |

## Schedule Types

Both agent tasks and database host jobs support these schedule types:

| Type | Value Format | Example |
|------|--------------|---------|
| `cron` | Cron expression | `0 9 * * 1` (Mondays at 9am) |
| `interval` | Milliseconds | `3600000` (every hour) |
| `once` | ISO timestamp | `2024-12-25T09:00:00Z` |

Config-file host cron jobs only support `cron`.
