# MCP Management

Pynchy provides centralized management of external MCP (Model Context Protocol) tool servers. `config.toml` is the single source of truth — adding a new MCP server is as simple as adding a TOML section.

## Architecture

```
config.toml
  ├── [mcp_servers]     → what exists (Docker or URL)
  ├── [mcp_groups]      → named sets for convenience
  ├── [mcp_presets]     → reusable kwarg bundles
  └── [workspaces.X]
        ├── mcp_servers → which MCPs this workspace can access
        └── [mcp.Y]    → per-MCP kwargs (become Docker flags)
              │
              ▼
┌─ McpManager ──────────────────────────────────────────┐
│  Boot:                                                │
│    1. Resolve workspace mcp_servers (expand groups)   │
│    2. Resolve presets into kwargs                     │
│    3. Compute unique (server, kwargs) instances       │
│    4. Register instances with LiteLLM via HTTP API    │
│    5. Create LiteLLM teams per workspace              │
│    6. Cache team IDs + keys                           │
│                                                       │
│  On-demand:                                           │
│    • Start Docker container when first agent needs it │
│    • Stop after idle_timeout                          │
└───────────────────────────────────────────────────────┘
        │                           │
        ▼                           ▼
  LiteLLM (:4000)           Docker MCP instances
  - /mcp endpoint            - pynchy-mcp-playwright-a3f2b1
  - Team → allowed_servers   - pynchy-mcp-playwright-7c1d4e
```

## Key concepts

**Instance deduplication.** Workspaces sharing the same (server, kwargs) naturally share one Docker container. Different kwargs produce different instances. Container naming: `pynchy-mcp-{server}-{hash_of_kwargs}`.

**On-demand lifecycle.** Docker MCP containers start when the first agent needs them and stop after `idle_timeout` seconds of inactivity. This keeps resource usage minimal.

**Per-workspace access control.** Each workspace gets a LiteLLM team with a virtual key scoped to its allowed MCP servers. The agent container receives this key and uses it to authenticate with the LiteLLM MCP endpoint.

**Kwargs as Docker flags.** Per-workspace MCP config (`[workspaces.X.mcp.Y]`) is arbitrary key-value pairs. For Docker MCPs, each becomes `--key value` appended to the container's args. Pynchy never interprets these — the MCP server itself enforces them (e.g., Playwright's `--allowed-origins`).

## Environment variables

Many MCP servers configure via environment variables rather than CLI args. Two fields on `McpServerConfig` support this:

**`env`** — static key-value pairs passed as `-e KEY=VALUE` to the Docker container. Use for non-secret configuration like bind addresses and ports.

**`env_forward`** (server level) — a list of host environment variable names. Each is resolved from `os.environ` at container start time and passed as `-e KEY=VALUE`. Use for secrets that live in `.env` on the host. If a variable is not set, pynchy logs a warning and skips it (the container still starts). The server-level list is an identity mapping: host var name = container var name.

**`env_forward`** (workspace level) — a dict mapping container var names to host var names. This overrides the server-level default, allowing different workspaces to source different host secrets for the same MCP server. Different env_forward mappings produce different container instances (separate hash → separate Docker container).

```toml
# Server defines what env vars the container needs (identity mapping by default)
[mcp_servers.example]
type = "docker"
image = "example/mcp-server:latest"
port = 8080
transport = "http"
env = { MCP_HOST = "0.0.0.0", MCP_PORT = "8080" }
env_forward = ["MCP_API_SECRET"]

# Single-tenant: workspace inherits identity mapping (MCP_API_SECRET → MCP_API_SECRET)
[workspaces.simple]
mcp_servers = ["example"]

# Multi-tenant: workspace remaps to a different host var
[workspaces.acme.mcp.example]
env_forward = { MCP_API_SECRET = "MCP_API_SECRET_ACME" }
```

The distinction keeps secrets out of `config.toml` (which is committed to the repo) while keeping non-secret config visible and declarative. The workspace-level mapping enables multi-tenant MCP: same server image, different credentials per workspace.

## Worked example: Slack MCP

[korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) provides read-only Slack access (channels, messages, users) via browser tokens. Here's how to add it:

### 1. Define the server in `config.toml`

```toml
[mcp_servers.slack_mcp]
type = "docker"
image = "ghcr.io/korotovsky/slack-mcp-server:latest"
port = 8080
transport = "http"
env = { SLACK_MCP_HOST = "0.0.0.0", SLACK_MCP_PORT = "8080" }
env_forward = ["SLACK_MCP_XOXC_TOKEN", "SLACK_MCP_XOXD_TOKEN"]
```

### 2. Add tokens to `.env`

Extract `xoxc` and `xoxd` browser tokens following the [upstream authentication guide](https://github.com/korotovsky/slack-mcp-server/blob/master/docs/01-authentication-setup.md). For multiple Slack workspaces, use distinct var names:

```
# Single workspace (matches server-level env_forward identity mapping)
SLACK_MCP_XOXC_TOKEN=xoxc-...
SLACK_MCP_XOXD_TOKEN=xoxd-...

# Multi-workspace: suffix per org
SLACK_XOXC_ACME=xoxc-...
SLACK_XOXD_ACME=xoxd-...
SLACK_XOXC_PERSONAL=xoxc-...
SLACK_XOXD_PERSONAL=xoxd-...
```

### 3. Grant workspace access

Single workspace (uses server-level identity mapping):

```toml
[workspaces.my-workspace]
mcp_servers = ["slack_mcp"]
```

Multiple Slack workspaces (each remaps to different host vars → separate containers):

```toml
[workspaces.acme-1]
mcp_servers = ["slack_mcp"]

[workspaces.acme-1.mcp.slack_mcp]
env_forward = { SLACK_MCP_XOXC_TOKEN = "SLACK_XOXC_ACME", SLACK_MCP_XOXD_TOKEN = "SLACK_XOXD_ACME" }

[workspaces.personal-1]
mcp_servers = ["slack_mcp"]

[workspaces.personal-1.mcp.slack_mcp]
env_forward = { SLACK_MCP_XOXC_TOKEN = "SLACK_XOXC_PERSONAL", SLACK_MCP_XOXD_TOKEN = "SLACK_XOXD_PERSONAL" }
```

The Slack MCP container starts on-demand when an agent in that workspace first needs it. Each workspace with different `env_forward` gets its own container instance. Tools like `channels_list`, `channels_history`, and `users_list` become available to the agent.

## Files

| File | Purpose |
|------|---------|
| `src/pynchy/config_mcp.py` | MCP config models (`McpServerConfig`) |
| `src/pynchy/container_runner/mcp_manager.py` | MCP lifecycle, LiteLLM sync, team provisioning |
| `src/pynchy/container_runner/_docker.py` | Shared Docker helpers |
