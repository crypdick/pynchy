# Container Isolation

How Pynchy isolates agents inside containers. Use this page to configure groups, debug mount issues, and write plugins that interact with the container filesystem.

Each agent invocation spawns a fresh, ephemeral container with explicitly mounted directories. The container runtime is pluggable — Pynchy ships with two built-in runtimes and picks one based on the platform. For the security properties of this isolation, see [Security Model](security.md).

## Container Runtime

The container runtime is pluggable via the `pynchy_container_runtime` hook. Pynchy auto-detects a runtime for the platform, or you can override in config:

```toml
[container]
runtime = "docker"    # or "apple"
```

### Built-in: Docker

The default runtime on Linux and the fallback on macOS. Requires the `docker` CLI.

### Built-in: Apple Container

The default runtime on macOS. Uses Apple's native container framework for lower overhead. Requires the `container` CLI (`brew install container`). Falls back to Docker if not installed.

## Container Lifecycle

Pynchy labels agent containers and removes stopped agent containers when it observes a session exit or starts the host service. Running or paused agent containers owned by an active in-process session stay protected. Unowned running or paused agent containers are reaped after `[container].orphan_reap_age_ms` (default: `604800000`, seven days). Startup cleanup also prunes dangling image layers while preserving tagged images. On Apple Container, Pynchy removes the BuildKit builder after Pynchy-controlled image builds so builder snapshots do not accumulate.

## Container Mounts

| Host Path | Container Path | Access | Groups |
|-----------|---------------|---------|--------|
| `groups/{name}/` | `/workspace/group` | Read-write | All |
| `data/sessions/{group}/.claude/` | `/home/agent/.claude` | Read-write | All (isolated per-group) |
| `data/sessions/{group}/.codex/` | `/home/agent/.codex` | Read-write | All (isolated per-group) |
| `src/pynchy/agent/scripts/` | `/workspace/scripts` | Readonly | All |
| `src/pynchy/agent/agent_runner/src` | `/app/src` | Readonly | All (agent runner source) |
| `data/ipc/{group}/` | `/workspace/ipc` | Read-write | All (IPC channel) |
| `data/env/{group}/` | `/workspace/env-dir` | Readonly | All (per-group credentials) |
| Obsidian vault root | `/workspace/vault` | Read-write | Learning-enabled groups |
| Repo worktrees | `/workspace/repos/<owner>/<repo>` | Read-write | Workspaces with profile `repo` |
| `{additional mounts}` | `/workspace/extra/*` | Configurable | Per containerConfig |

**Notes:**

- Workspaces with profile `repo` receive worktree mounts at `/workspace/repos/<owner>/<repo>` (see [Worktrees](../usage/worktrees.md))
- Automatic learning mounts the configured Obsidian vault root at `/workspace/vault` by default. That vault root acts as the global memory namespace.
- Shared agent instructions are delivered via [prompts](../usage/prompts.md), not filesystem mounts
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

**LLM credentials** flow through the host gateway (see [Security Model](security.md#6-credential-handling)). Containers receive gateway URLs and an ephemeral key — never real API keys:

- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` — points to host gateway
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` — points to host gateway

**Non-LLM credentials** get written directly, scoped by trust level:

- `GH_TOKEN` — **admin containers only.** Auto-discovered from `gh auth token` or `config.toml [secrets]`. Non-admin containers don't receive this; their git operations are routed through host IPC.
- `GIT_AUTHOR_NAME` / `GIT_COMMITTER_NAME` — from host git config (all groups)
- `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_EMAIL` — from host git config (all groups)

**Process:**
1. Host discovers credentials from `config.toml [secrets]` and auto-discovery (OAuth, gh CLI, git config)
2. LLM keys are registered with the gateway; containers get the gateway URL + ephemeral key
3. `GH_TOKEN` is included only for admin containers
4. Per-group env file written to `data/env/{group}/env`
5. Env material is mounted into the container
6. Container entrypoint sources the env file

---

**Want to customize this?** Write your own container runtime plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
