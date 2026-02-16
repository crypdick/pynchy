# Container Isolation

All agents run inside containers — Apple Container (macOS, preferred) or Docker (macOS/Linux). Each agent invocation spawns a fresh, ephemeral container with explicitly mounted directories.

For security properties of container isolation, see [Security Model](security.md).

## Container Mounts

| Host Path | Container Path | Access | Groups |
|-----------|---------------|---------|--------|
| `groups/{name}/` | `/workspace/group` | Read-write | All |
| `groups/global/` | `/workspace/global` | Readonly | Non-god only |
| `data/sessions/{group}/.claude/` | `/home/agent/.claude` | Read-write | All (isolated per-group) |
| `container/scripts/` | `/workspace/scripts` | Readonly | All |
| `container/agent_runner/src` | `/app/src` | Readonly | All (agent runner source) |
| `data/ipc/{group}/` | `/workspace/ipc` | Read-write | All (IPC channel) |
| `data/env/` | `/workspace/env-dir` | Readonly | All (credentials) |
| `{additional mounts}` | `/workspace/extra/*` | Configurable | Per containerConfig |

**Notes:**
- Groups with `project_access` get worktree mounts instead of `groups/global/` (see `.claude/worktrees.md`)
- Apple Container requires `--mount "type=bind,source=...,target=...,readonly"` syntax for readonly mounts (`:ro` suffix doesn't work)

## Container Configuration

Groups can have additional directories mounted via `containerConfig` in the SQLite `registered_groups` table:

```json
{
  "additional_mounts": [
    {
      "host_path": "~/projects/webapp",
      "container_path": "webapp",
      "readonly": false
    }
  ],
  "timeout": 600000
}
```

## Environment Variable Isolation

Only allowlisted variables are exposed to containers. The `.env` file can contain various variables, but only specific ones are mounted:

**Extracted Variables (from `.env`):**
- `CLAUDE_CODE_OAUTH_TOKEN` — OAuth token from `~/.claude/.credentials.json` (subscription)
- `ANTHROPIC_API_KEY` — API key for Claude access (pay-per-use)
- `GH_TOKEN` — GitHub token (also auto-discovered from `gh auth token`)
- `OPENAI_API_KEY` — OpenAI API key

**Auto-Discovered Variables:**
- `GIT_AUTHOR_NAME` / `GIT_COMMITTER_NAME` — from host git config
- `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_EMAIL` — from host git config

**Process:**
1. Host reads `.env` and extracts only allowlisted variables
2. Auto-discovers credentials (OAuth token, GH token, git identity) if not in `.env`
3. Filtered variables are written to `data/env/env`
4. This file is mounted into containers at `/workspace/env-dir/env`
5. Container entrypoint sources the file
