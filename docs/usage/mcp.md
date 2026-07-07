# MCP Servers

Add external tool servers to pynchy using the [Model Context Protocol](https://modelcontextprotocol.io/). MCP servers give agents tools beyond the built-ins — Slack, Playwright, databases, or any service with an MCP server.

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

## Script MCP servers

`type = "script"` runs an MCP server as a host subprocess instead of a Docker container. Useful for tools that need host access or use `uv run` with ad-hoc dependencies.

```toml
[mcp_servers.my_tool]
type = "script"
command = "uv"
args = ["run", "scripts/my-tool.py"]
port = 8080
transport = "streamable_http"
idle_timeout = 600
```

Script MCP servers share the Docker MCP lifecycle: start on-demand, stop after `idle_timeout` seconds of inactivity. The difference is they run directly on the host (no container isolation) and LiteLLM reaches them via `localhost` instead of the Docker network.

When to use scripts over Docker:

- The tool needs host filesystem access (e.g., writing to `.env`)
- You're using `uv run` with PEP 723 inline dependencies
- The tool isn't packaged as a Docker image

Script MCPs support the same `env`, `env_forward`, and per-workspace kwargs as Docker MCPs.

Plugins can also provide script MCP servers via the `pynchy_mcp_server_spec()` hook — those appear automatically without config.toml entries. Config.toml definitions override plugin defaults if names collide.

## Environment variables

Many MCP servers configure via env vars rather than CLI args. Two fields on `McpServerConfig` handle this:

**`env`** — static key-value pairs passed as `-e KEY=VALUE` to the Docker container. Use for non-secret config like bind addresses and ports.

**`env_forward`** — maps container env var names to host env var names. Two forms:

- **List** (identity mapping): `env_forward = ["API_KEY"]` — host var and container var share the same name.
- **Dict** (explicit mapping): `env_forward = { CONTAINER_VAR = "HOST_VAR" }` — the container sees `CONTAINER_VAR`, resolved from `HOST_VAR` in the host's `.env`.

If a host var is unset, pynchy logs a warning and skips it (the container still starts).

The `env`/`env_forward` split keeps secrets out of `config.toml` (which is committed) while keeping non-secret config visible and declarative. Changes to `.env` trigger an automatic service restart.

Prefer OneCLI for secrets that leave the host. Set `onecli = true` on a Docker
MCP server to pass OneCLI proxy env and CA material into the sidecar. If the MCP
server needs a local credential file to start, create a stub file containing
`onecli-managed` placeholders and let OneCLI inject the real credential when the
server makes an outbound HTTP request. Use `env_forward` for native
compatibility or non-secret host values, not as the first-choice credential
boundary.

## Multi-tenant servers

To run the same MCP server image against different accounts (e.g., multiple Slack workspaces), define separate server entries with different `env_forward` mappings:

```toml
[mcp_servers.example_acme]
type = "docker"
image = "example/mcp-server:latest"
port = 8080
transport = "http"
env = { MCP_HOST = "0.0.0.0", MCP_PORT = "8080" }
onecli = true
env_forward = { MCP_API_SECRET = "MCP_API_SECRET_ACME" }  # pragma: allowlist secret

[mcp_servers.example_personal]
type = "docker"
image = "example/mcp-server:latest"
port = 8081
transport = "http"
env = { MCP_HOST = "0.0.0.0", MCP_PORT = "8081" }
onecli = true
env_forward = { MCP_API_SECRET = "MCP_API_SECRET_PERSONAL" }  # pragma: allowlist secret
```

## Persistent volumes

Docker MCP containers are ephemeral by default — data is lost when they stop. To persist caches or other state across restarts, use `volumes`:

```toml
[mcp_servers.slack_mcp_acme]
# ...
volumes = ["data/mcp-cache/slack-acme:/root/.cache/slack-mcp-server"]
```

Relative host paths resolve from the project root. The host directory is created if it doesn't exist.

## Per-workspace kwargs

Per-workspace MCP config (`[workspaces.X.mcp.Y]`) is arbitrary key-value pairs. For Docker MCPs, each one becomes `--key value` appended to the container's args. Pynchy never interprets them — the MCP server itself enforces them (e.g., Playwright's `--allowed-origins`).

## Server-specific guides

| Server | Guide |
|--------|-------|
| Google Drive | [Google Drive setup](gdrive.md) |
| Notebooks | [Notebook execution](notebooks.md) |
| Slack MCP | [Slack MCP setup](slack-mcp.md) |

For architecture internals (instance deduplication, LiteLLM integration, access control), see [MCP management architecture](../architecture/mcp-management.md).
