# MCP Servers

Add external tool servers to pynchy using the [Model Context Protocol](https://modelcontextprotocol.io/). MCP servers give agents tools beyond the built-ins — Slack, Playwright, databases, or any service with an MCP server.

## Built-in browser server

Pynchy ships a built-in `browser` MCP server backed by `@playwright/mcp`. It
runs as a host subprocess so browser windows appear on the host desktop. The
built-in browser is headed by default, which matches modern anti-bot reality
better than forcing Playwright's headless mode.

Set `PYNCHY_BROWSER_HEADLESS=true` in the host environment only when the host has
no display or you deliberately want the older headless behavior.

## Adding a server

Define it in `config.toml`:

```toml
[tools.playwright]
type = "mcp"
public_source = true
secret_data = false
public_sink = false
dangerous_writes = false

[tools.playwright.mcp]
runtime = "docker"
image = "mcr.microsoft.com/playwright/mcp:latest"
port = 8931
args = ["--port", "{port}", "--host", "0.0.0.0"]
```

Then grant workspace access:

```toml
[profiles.browser]
tools = ["playwright"]

[workspaces.my-workspace]
profiles = ["browser"]
```

MCP runtime details come from plugins and server-specific setup flows. The profile controls which MCP-backed tool names a workspace may use, while the trust fields control security gating.

## Multi-tenant Servers

For multiple accounts, define one tool per account and compose the right tool names into profiles:

```toml
[tools.example_acme]
type = "mcp"
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[tools.example_acme.mcp]
runtime = "url"
url = "https://acme.example.com/mcp"

[tools.example_personal]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools.example_personal.mcp]
runtime = "url"
url = "https://personal.example.com/mcp"

[profiles.work]
tools = ["example_acme"]

[profiles.personal]
tools = ["example_personal"]
```

## Server-specific guides

| Server | Guide |
|--------|-------|
| Google Drive | [Google Drive setup](gdrive.md) |
| Linear | [Linear task tracking](linear.md) |
| Notebooks | [Notebook execution](notebooks.md) |
| Proton Mail | [Proton Mail](proton-mail.md) |
| Slack MCP | [Slack MCP setup](slack-mcp.md) |

For architecture internals (instance deduplication, LiteLLM integration, access control), see [MCP management architecture](../architecture/mcp-management.md).
