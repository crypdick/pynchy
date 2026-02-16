# Container Isolation

This page describes how Pynchy isolates agents inside containers. Understanding the mount layout and environment variable setup helps you configure groups, debug mount issues, and write plugins that interact with the container filesystem.

Each agent invocation spawns a fresh, ephemeral container (Apple Container on macOS, Docker on Linux) with explicitly mounted directories. For the security properties of this isolation, see [Security Model](security.md).

## Container Mounts

| Host Path | Container Path | Access | Groups |
|-----------|---------------|---------|--------|
| `groups/{name}/` | `/workspace/group` | Read-write | All |
| `groups/global/` | `/workspace/global` | Readonly | Non-god only |
| `data/sessions/{group}/.claude/` | `/home/agent/.claude` | Read-write | All (isolated per-group) |
| `container/scripts/` | `/workspace/scripts` | Readonly | All |
| `container/agent_runner/src` | `/app/src` | Readonly | All (agent runner source) |
| `data/ipc/{group}/` | `/workspace/ipc` | Read-write | All (IPC channel) |
| `data/env/{group}/` | `/workspace/env-dir` | Readonly | All (per-group credentials) |
| `config.toml` | `/workspace/project/config.toml` | Read-write | God only |
| `{additional mounts}` | `/workspace/extra/*` | Configurable | Per containerConfig |

**Notes:**
- Groups with `project_access` receive worktree mounts instead of `groups/global/` (see `.claude/worktrees.md`)
- Apple Container requires `--mount "type=bind,source=...,target=...,readonly"` syntax for readonly mounts (the `:ro` suffix does not work)

## Container Configuration

Configure additional directory mounts via `containerConfig` in the SQLite `registered_groups` table:

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

Each group gets its own env file at `data/env/{group}/env`. Only allowlisted variables pass through.

**LLM credentials** flow through the host gateway (see [Security Model](security.md#5-credential-handling)). Containers receive gateway URLs and an ephemeral key — never real API keys:
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` — points to host gateway
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` — points to host gateway

**Non-LLM credentials** get written directly, scoped by trust level:
- `GH_TOKEN` — **god containers only.** Auto-discovered from `gh auth token` or `config.toml [secrets]`. Non-god containers don't receive this; their git operations are routed through host IPC.
- `GIT_AUTHOR_NAME` / `GIT_COMMITTER_NAME` — from host git config (all groups)
- `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_EMAIL` — from host git config (all groups)

**Process:**
1. Host discovers credentials from `config.toml [secrets]` and auto-discovery (OAuth, gh CLI, git config)
2. LLM keys are registered with the gateway; containers get the gateway URL + ephemeral key
3. `GH_TOKEN` is included only for god containers
4. Per-group env file written to `data/env/{group}/env`
5. Mounted into the container at `/workspace/env-dir/env`
6. Container entrypoint sources the file
