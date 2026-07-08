# Linear

The built-in Linear integration provides task-tracking tools to agents through a
script MCP server. It lets an agent list Linear teams, list recent issues, and
create issues in a selected team.

## Configure access

Create a Linear personal API key, then store it in the host environment as
`LINEAR_API_KEY`. Pynchy forwards that variable into the Linear MCP server when a
workspace enables the server.

Enable the server for a workspace:

```toml
[workspaces.pynchy-dev]
mcp_servers = ["linear"]
```

The plugin supplies the `[mcp_servers.linear]` definition automatically, so the
host config only needs the workspace grant.

## Available tools

| Tool | Purpose |
|------|---------|
| `linear_list_teams` | Lists Linear teams visible to the API key. Use this first to find the `team_id`. |
| `linear_list_issues` | Lists recent issues, optionally scoped by `team_id`. |
| `linear_create_issue` | Creates an issue with `team_id`, `title`, and optional `description`, `project_id`, `state_id`, and `label_ids`. |

The integration marks Linear as a public sink because issue creation sends data
to Linear. Workspace security policy can still require approval before agents
use the tool.

## Backlog migration

This integration only adds the Linear task-tracking capability. Migrating
`backlog/TODO.md` and its plan files into Linear remains separate follow-up work.
