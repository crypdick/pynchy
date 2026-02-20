# MCP Servers

This page covers how to add external tool servers to pynchy using the [Model Context Protocol](https://modelcontextprotocol.io/). MCP servers give your agents access to tools beyond what's built in — Slack, Playwright, databases, or any service with an MCP server.

## Adding a server

Define it in `config.toml`:

```toml
[mcp_servers.playwright]
type = "docker"
image = "mcr.microsoft.com/playwright/mcp:latest"
port = 8080
transport = "http"
```

Then grant workspace access:

```toml
[workspaces.my-workspace]
mcp_servers = ["playwright"]
```

Docker MCP containers start on-demand when an agent first needs them and stop after `idle_timeout` seconds of inactivity.

## Environment variables

Many MCP servers configure via environment variables rather than CLI args. Two fields on `McpServerConfig` support this:

**`env`** — static key-value pairs passed as `-e KEY=VALUE` to the Docker container. Use for non-secret configuration like bind addresses and ports.

**`env_forward`** — maps container env var names to host env var names. Accepts two forms:
- **List** (identity mapping): `env_forward = ["API_KEY"]` — host var and container var share the same name.
- **Dict** (explicit mapping): `env_forward = { CONTAINER_VAR = "HOST_VAR" }` — the container sees `CONTAINER_VAR`, resolved from `HOST_VAR` in the host's `.env`.

If a host var is not set, pynchy logs a warning and skips it (the container still starts).

The `env`/`env_forward` split keeps secrets out of `config.toml` (which is committed to the repo) while keeping non-secret config visible and declarative. Changes to `.env` trigger an automatic service restart.

## Multi-tenant servers

For the same MCP server image connecting to different accounts (e.g., multiple Slack workspaces), define separate server entries with different `env_forward` mappings:

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

## Per-workspace kwargs

Per-workspace MCP config (`[workspaces.X.mcp.Y]`) is arbitrary key-value pairs. For Docker MCPs, each becomes `--key value` appended to the container's args. Pynchy never interprets these — the MCP server itself enforces them (e.g., Playwright's `--allowed-origins`).

## Server-specific guides

| Server | Guide |
|--------|-------|
| Slack MCP | [Slack MCP setup](slack-mcp.md) |

For architecture internals (instance deduplication, LiteLLM integration, access control), see [MCP management architecture](../architecture/mcp-management.md).
