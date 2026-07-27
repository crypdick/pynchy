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

Define it in `data/personalization/pynchy.toml`:

```toml
[tools.playwright]
type = "mcp"
skills = ["browser-control"]
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

MCP runtime details come from plugins and server-specific setup flows. The
profile controls which MCP-backed tool names a workspace may use, while the
trust fields control security gating. Selecting the tool installs its companion
skills. See [Tool access and secrets](tool-access.md) for credential
requirements and process exposure.

## Host stdio servers

Use `runtime = "stdio"` for a trusted host command that implements MCP over
standard input and output. Pynchy exposes it only through a loopback
Streamable HTTP bridge; the agent still reaches it through Pynchy's security
proxy.

```toml
[tools.android]
type = "mcp"
required_env = ["ANDROID_TOKEN"]
public_source = false
secret_data = true
public_sink = false
dangerous_writes = true

[tools.android.mcp]
runtime = "stdio"
command = "/path/to/npx"
args = ["-y", "scrcpy-mcp"]
port = 8932
transport = "streamable_http"
env = { ADB_PATH = "/path/to/adb" }
```

`dangerous_writes = true` keeps every tool call subject to the normal human
approval flow. The bridge receives only a small host environment. Put non-secret
constants in `mcp.env`, and declare secret names on the tool. The selected tool
process receives `ANDROID_TOKEN`; unrelated host variables do not. Pynchy does
not support `env_forward`. See
[Tool access and secrets](tool-access.md#define-tools) for the complete model.

For the lifecycle and routing details, see [MCP management architecture](../architecture/mcp-management.md#host-stdio-servers).

## Multi-tenant Servers

For multiple accounts, define one tool per account and compose the right tool
names into profiles. Give each account distinct requirement names when its
runtime supports them:

```toml
[tools.example_acme]
type = "mcp"
required_env = ["EXAMPLE_ACME_TOKEN"]
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[tools.example_acme.mcp]
runtime = "url"
url = "https://acme.example.com/mcp"
auth_value_env = "EXAMPLE_ACME_TOKEN"

[tools.example_personal]
type = "mcp"
required_env = ["EXAMPLE_PERSONAL_TOKEN"]
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools.example_personal.mcp]
runtime = "url"
url = "https://personal.example.com/mcp"
auth_value_env = "EXAMPLE_PERSONAL_TOKEN"

[profiles.work]
tools = ["example_acme"]

[profiles.personal]
tools = ["example_personal"]
```

## Server-specific guides

| Server | Guide |
|--------|-------|
| Google Drive | [Google Drive setup](../integrations/google/drive.md) |
| Linear | [Linear task tracking](../integrations/linear.md) |
| Notebooks | [Notebook execution](../integrations/notebooks.md) |
| Proton Mail | [Proton Mail](../integrations/proton-mail.md) |
| Slack MCP | [Slack MCP setup](../integrations/slack-mcp.md) |

For architecture internals (instance deduplication, LiteLLM integration, access control), see [MCP management architecture](../architecture/mcp-management.md).
