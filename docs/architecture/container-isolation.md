# Container Isolation

All agents run inside containers — Apple Container (macOS, preferred) or Docker (macOS/Linux). Each agent invocation spawns a fresh, ephemeral container with explicitly mounted directories.

For security properties of container isolation, see [Security Model](../security.md).

## Container Mounts

| Host Path | Container Path | Access | Groups |
|-----------|---------------|---------|--------|
| `groups/{name}/` | `/workspace/group` | Read-write | All |
| `groups/global/` | `/workspace/global` | Readonly | Non-god only |
| `data/sessions/{group}/.claude/` | `/home/agent/.claude` | Read-write | All (isolated per-group) |
| `container/scripts/` | `/workspace/scripts` | Readonly | All |
| `{additional mounts}` | `/workspace/extra/*` | Configurable | Per containerConfig |

**Notes:**
- Groups with `project_access` get worktree mounts instead of `groups/global/` (see `.claude/worktrees.md`)
- Apple Container requires `--mount "type=bind,source=...,target=...,readonly"` syntax for readonly mounts (`:ro` suffix doesn't work)

## Container Configuration

Groups can have additional directories mounted via `containerConfig` in the SQLite `registered_groups` table:

```json
{
  "additionalMounts": [
    {
      "hostPath": "~/projects/webapp",
      "containerPath": "webapp",
      "readonly": false
    }
  ],
  "timeout": 600000
}
```

## Environment Variable Isolation

Only authentication variables are exposed to containers. The `.env` file can contain various variables, but only specific ones are mounted:

**Extracted Variables:**
- `ANTHROPIC_API_KEY` — API key for Claude access (pay-per-use)
- `CLAUDE_CODE_OAUTH_TOKEN` — OAuth token from `~/.claude/.credentials.json` (subscription)

**Process:**
1. Host reads `.env` and extracts only authentication variables
2. Filtered variables are written to `data/env/env`
3. This file is mounted into containers at `/workspace/env-dir/env`
4. Container entrypoint sources the file

Other environment variables (API keys for other services, personal tokens, etc.) are never exposed to agents.
