# Linear

The built-in Linear integration gives every Pynchy workspace a durable Linear
todo board. Each workspace maps to a Linear Project named from the workspace
label, using the folder title for repo-slug labels (`Code Improver` for
`code-improver`), and todos move through shared Linear workflow states: Backlog,
Planning, Ready, In Progress, and Done.

## Configure access

Create a Linear personal API key, then store it in the host environment as
`LINEAR_API_KEY`. If that key can see exactly one Linear team, no other config is
needed. Pynchy creates missing workspace projects and workflow states on boot.

If the key can see multiple teams, set `LINEAR_TEAM_KEY` to the team key, team
ID, or exact team name Pynchy should use:

```bash
LINEAR_API_KEY=lin_api_...
LINEAR_TEAM_KEY=SYN
```

The plugin supplies the `[mcp_servers.linear]` definition automatically. When
`LINEAR_API_KEY` is present, Pynchy also attaches the Linear MCP server to every
workspace by default, so individual sandbox config does not need
`mcp_servers = ["linear"]`.

Editing `.env` triggers the normal Pynchy auto-restart; do not restart the
service manually unless the health check shows it is stuck.

## Workspace boards

On boot, Pynchy reconciles Linear state for all registered workspaces:

| Pynchy workspace | Linear object |
|------------------|---------------|
| Workspace label | Project named from the workspace, such as `Code Improver` |
| `todo ...` messages | Issues in that workspace project |
| Todo status | Team workflow state |

Boot reconciliation is additive only. Pynchy creates missing projects and states,
and renames older Pynchy-managed projects when their description contains the
matching `pynchy.workspace=...` marker. It does not delete, archive, assign, or
otherwise clean up Linear objects automatically.

When a user sends `todo ...` while a workspace task is running, Pynchy still
writes the local todo cache and also creates a Linear issue in the workspace
project when Linear is configured.

## Available tools

| Tool | Purpose |
|------|---------|
| `linear_list_teams` | Lists Linear teams visible to the API key. Use this first to find the `team_id`. |
| `linear_list_issues` | Lists recent issues, optionally scoped by `team_id`. |
| `linear_create_issue` | Creates an issue with `team_id`, `title`, and optional `description`, `project_id`, `state_id`, and `label_ids`. |
| `linear_list_todos` | Lists open Linear todo issues for the current Pynchy workspace. |
| `linear_create_todo` | Creates a workspace todo issue, defaulting to Backlog. |
| `linear_move_todo` | Moves a workspace todo through `backlog`, `planning`, `ready`, `in_progress`, or `done`. |

The integration marks Linear as a public sink because issue creation sends data
to Linear. Workspace security policy can still require approval before agents
use the tool.

## Backlog migration

This integration only adds the Linear task-tracking capability. Migrating
`backlog/TODO.md` and its plan files into Linear remains separate follow-up work.
