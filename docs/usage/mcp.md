# MCP Servers

Add external tool servers to pynchy using the [Model Context Protocol](https://modelcontextprotocol.io/). MCP servers give agents tools beyond the built-ins — Slack, Playwright, databases, or any service with an MCP server.

## Adding a server

Define it in `config.toml`:

```toml
[tools.playwright]
type = "mcp"
public_source = true
secret_data = false
public_sink = false
dangerous_writes = false
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

[tools.example_personal]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

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
| Slack MCP | [Slack MCP setup](slack-mcp.md) |

For architecture internals (instance deduplication, LiteLLM integration, access control), see [MCP management architecture](../architecture/mcp-management.md).
