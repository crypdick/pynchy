# Container Isolation

How Pynchy isolates agents inside containers. Use this page to configure groups, debug mount issues, and write plugins that interact with the container filesystem.

Each agent invocation spawns a fresh, ephemeral container with explicitly mounted directories. The container runtime is pluggable — Pynchy ships with Docker, Apple Container, and Kubernetes runtimes. For the security properties of this isolation, see [Security Model](security.md).

## Container Runtime

The container runtime is pluggable via the `pynchy_container_runtime` hook. Pynchy auto-detects a runtime for the platform, or you can override in config:

```toml
[container]
runtime = "docker"    # or "apple" or "kubernetes"
```

### Built-in: Docker

The default runtime on Linux and the fallback on macOS. Requires the `docker` CLI.

### Built-in: Apple Container

The default runtime on macOS. Uses Apple's native container framework for lower overhead. Requires the `container` CLI (`brew install container`). Falls back to Docker if not installed.

### Built-in: Kubernetes

An explicit runtime for a Pynchy host running in Kubernetes. It translates the
Docker-compatible command subset used by Pynchy into namespace-scoped Pods and
Services. Mount sources must be under `PYNCHY_KUBERNETES_SHARED_ROOT` and are
projected from `PYNCHY_KUBERNETES_PVC`; arbitrary host paths are rejected. See
the [Kubernetes installation guide](../installation/kubernetes.md).

## Container Lifecycle

Pynchy labels agent containers and removes stopped agent containers when it observes a session exit or starts the host service. Running or paused agent containers owned by an active in-process session stay protected. Unowned running or paused agent containers are reaped after `[container].orphan_reap_age_ms` (default: `604800000`, seven days). Before startup image validation, Pynchy prunes dangling image layers while preserving tagged images and a healthy Apple Container BuildKit cache. A failed Apple Container build discards its builder before the next attempt. Timed-out host commands first receive `SIGTERM` as a process group so shell cleanup traps can run, then receive `SIGKILL` only if the group outlives the grace period.

## Container Mounts

| Host Path | Container Path | Access | Groups |
|-----------|---------------|---------|--------|
| `groups/{name}/` | `/home/agent/workspace` | Read-write | All |
| `data/sessions/{group}/.claude/` | `/home/agent/.claude` | Read-write | All (isolated per-group) |
| `data/sessions/{group}/.codex/` | `/home/agent/.codex` | Read-write | All (isolated per-group) |
| `src/pynchy/agent/scripts/` | `/opt/pynchy/scripts` | Readonly | All |
| `src/pynchy/agent/agent_runner/src` | `/opt/pynchy/agent-runner/src` | Readonly | All (agent runner source) |
| `data/ipc/{group}/` | `/run/pynchy` | Read-write | All (IPC channel) |
| Obsidian vault root | `/home/agent/memory` | Read-write | Learning-enabled groups |
| Repo worktrees | `/home/agent/src/<owner>/<repo>` | Read-write | Workspaces with profile `repo` |
| `{additional mounts}` | `/home/agent/mnt/*` | Configurable | Per containerConfig |

**Notes:**

- Agent working files live under `/home/agent`: the group workspace is `/home/agent/workspace`, and repository worktrees are `/home/agent/src/<owner>/<repo>` (see [Worktrees](../usage/worktrees.md)).
- Harness files live outside the agent home: runner code and scripts are under `/opt/pynchy`, while IPC is under `/run/pynchy`.
- Automatic learning mounts the configured Obsidian vault root at `/home/agent/memory` by default. That vault root acts as the global memory namespace.
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

Pynchy constructs each agent process environment from a small operational
baseline plus variables authorized by selected tools. It never generates a
workspace env file or mounts an environment directory.

**LLM credentials** flow through the host gateway (see [Security Model](security.md#6-credential-handling)). Containers receive gateway URLs and an ephemeral key — never real API keys:

- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` — points to host gateway
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` — points to host gateway

**Non-LLM process values** follow explicit boundaries:

- `GIT_AUTHOR_NAME` / `GIT_COMMITTER_NAME` — from host git config (all groups)
- `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_EMAIL` — from host git config (all groups)
- A `type = "workspace"` tool's declared variables enter the selected agent
  workspace.
- Runtime-backed tool variables stay in the tool process unless the declaration
  sets `expose_env_to_workspace = true`.

**Process:**

1. The host resolves the workspace's selected TOML tools.
2. Missing requirements disable only the affected tools.
3. LLM keys stay in the gateway; agent containers receive its URL and
   ephemeral key.
4. The container runtime receives value-free `-e NAME` flags for selected
   workspace variables.
5. The container CLI subprocess receives the corresponding values through its
   filtered environment.

See [Tool access and secrets](../usage/tool-access.md) for declarations,
companion skills, missing-access notices, and host secret materialization.

---

**Want to customize this?** Write your own container runtime plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
