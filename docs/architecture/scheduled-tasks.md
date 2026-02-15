# Scheduled Tasks

Users can ask Claude to schedule recurring or one-time tasks from any group. Tasks run as full agents in the context of the group that created them.

## Schedule Types

| Type | Value Format | Example |
|------|--------------|---------|
| `cron` | Cron expression | `0 9 * * 1` (Mondays at 9am) |
| `interval` | Milliseconds | `3600000` (every hour) |
| `once` | ISO timestamp | `2024-12-25T09:00:00Z` |

## Capabilities

- Tasks have access to all tools including Bash (safe in container)
- Tasks can optionally send messages to their group via `send_message` tool, or complete silently
- Task runs are logged to the database with duration and result
- From god container: can schedule tasks for any group, view/manage all tasks
- From other groups: can only manage that group's tasks

## MCP Tools (pynchy server)

| Tool | Purpose |
|------|---------|
| `schedule_task` | Schedule a recurring or one-time task |
| `list_tasks` | Show tasks (group's tasks, or all if god) |
| `get_task` | Get task details and run history |
| `update_task` | Modify task prompt or schedule |
| `pause_task` | Pause a task |
| `resume_task` | Resume a paused task |
| `cancel_task` | Delete a task |
| `send_message` | Send a WhatsApp message to the group |
