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

**`env_forward`** — maps container env var names to host env var names. Accepts two forms:
- **List** (identity mapping): `env_forward = ["API_KEY"]` — host var and container var share the same name.
- **Dict** (explicit mapping): `env_forward = { CONTAINER_VAR = "HOST_VAR" }` — the container sees `CONTAINER_VAR`, resolved from `HOST_VAR` in the host's `.env`.

If a host var is not set, pynchy logs a warning and skips it (the container still starts).

For multi-tenant MCP (e.g., multiple Slack workspaces), define separate server entries with different `env_forward` mappings:

```toml
[mcp_servers.example_acme]
type = "docker"
image = "example/mcp-server:latest"
port = 8080
transport = "http"
env = { MCP_HOST = "0.0.0.0", MCP_PORT = "8080" }
env_forward = { MCP_API_SECRET = "MCP_API_SECRET_ACME" }

[mcp_servers.example_personal]
type = "docker"
image = "example/mcp-server:latest"
port = 8081
transport = "http"
env = { MCP_HOST = "0.0.0.0", MCP_PORT = "8081" }
env_forward = { MCP_API_SECRET = "MCP_API_SECRET_PERSONAL" }
```

The `env`/`env_forward` split keeps secrets out of `config.toml` (which is committed to the repo) while keeping non-secret config visible and declarative.

## Worked example: Slack MCP

[korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) provides read-only Slack access (channels, messages, users) via browser tokens. Here's how to add it:

### 1. Define the server in `config.toml`

Each Slack workspace gets its own server entry with its own token mapping:

```toml
[mcp_servers.slack_mcp_acme]
type = "docker"
image = "ghcr.io/korotovsky/slack-mcp-server:latest"
port = 8080
transport = "http"
env = { SLACK_MCP_HOST = "0.0.0.0", SLACK_MCP_PORT = "8080" }
env_forward = { SLACK_MCP_XOXC_TOKEN = "SLACK_XOXC_ACME", SLACK_MCP_XOXD_TOKEN = "SLACK_XOXD_ACME" }

[mcp_servers.slack_mcp_personal]
type = "docker"
image = "ghcr.io/korotovsky/slack-mcp-server:latest"
port = 8081
transport = "http"
env = { SLACK_MCP_HOST = "0.0.0.0", SLACK_MCP_PORT = "8081" }
env_forward = { SLACK_MCP_XOXC_TOKEN = "SLACK_XOXC_PERSONAL", SLACK_MCP_XOXD_TOKEN = "SLACK_XOXD_PERSONAL" }
```

### 2. Add tokens to `.env`

Extract `xoxc` and `xoxd` browser tokens following the [upstream authentication guide](https://github.com/korotovsky/slack-mcp-server/blob/master/docs/01-authentication-setup.md):

```
SLACK_XOXC_ACME=xoxc-...
SLACK_XOXD_ACME=xoxd-...
SLACK_XOXC_PERSONAL=xoxc-...
SLACK_XOXD_PERSONAL=xoxd-...
```

### 3. Grant workspace access

```toml
[workspaces.acme-1]
mcp_servers = ["slack_mcp_acme"]

[workspaces.personal-1]
mcp_servers = ["slack_mcp_personal"]
```

Each server entry gets its own Docker container with its own tokens. Containers start on-demand when an agent first needs them. Tools like `channels_list`, `channels_history`, and `users_list` become available to the agent.

## Files

| File | Purpose |
|------|---------|
| `src/pynchy/config_mcp.py` | MCP config models (`McpServerConfig`) |
| `src/pynchy/container_runner/mcp_manager.py` | MCP lifecycle, LiteLLM sync, team provisioning |
| `src/pynchy/container_runner/_docker.py` | Shared Docker helpers |
