# Available Plugins

This page tracks plugins that work with pynchy.

## Built-in Plugins

These ship with pynchy and are always available. Some require optional dependencies (`uv sync --extra <name>`) and activate only when their config section is present:

| Plugin | Type | Purpose | Config |
|--------|------|---------|--------|
| `agent_claude` | Agent Core | Default Claude SDK agent core. | Always active |
| `agent_openai` | Agent Core | OpenAI Agents SDK alternative. | `PYNCHY_AGENT_CORE=openai` |
| `whatsapp` | Channel | WhatsApp channel via neonize. | `uv sync --extra whatsapp` + QR auth |
| `slack` | Channel | Slack channel via Socket Mode (bolt). Maps Slack channels/DMs to workspaces. | `[slack] bot_token / app_token` + `uv sync --extra slack` |
| `caldav` | MCP Server Handler | CalDAV calendar tools (list, create, delete events). Works with Nextcloud and other CalDAV servers. | `[caldav] url / username / password` + `uv sync --extra caldav` |
| `apple-runtime` | Container Runtime | Apple Container runtime for macOS hosts. | macOS only (auto-detected) |
| `tailscale` | Tunnel | Tailscale connectivity detection. Warns at startup if tunnel is down. | Always active (requires `tailscale` CLI) |

Plugins with optional dependencies are gracefully skipped at startup if their dependencies aren't installed. Install all optional dependencies at once with `uv sync --extra all`.

## Third-Party Plugins

Third-party plugins are discovered automatically via Python entry points. Install a plugin package and restart pynchy — no config needed.

To add your plugin to this registry:

1. Build your plugin using the [plugin creation guide](quickstart.md).
2. Open a PR that updates this page with your plugin entry.

Include:

- Plugin name (entry-point key)
- Short description
- Public repository URL
