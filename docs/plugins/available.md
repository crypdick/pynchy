# Available Plugins

This page tracks plugins that work with pynchy.

## Built-in Plugins

These ship with pynchy and are always available. Some require optional dependencies (`uv sync --extra <name>`) and activate only when their config section is present:

| Plugin | Type | Purpose | Config | Docs |
|--------|------|---------|--------|------|
| `agent_claude` | Agent Core | Default Claude SDK agent core. | Always active | [Agent cores](../usage/agent-cores.md) |
| `agent_openai` | Agent Core | OpenAI Agents SDK alternative. | `PYNCHY_AGENT_CORE=openai` | [Agent cores](../usage/agent-cores.md) |
| `whatsapp` | Channel | WhatsApp channel via neonize. | `uv sync --extra whatsapp` + QR auth | [Channels](../usage/channels.md) |
| `slack` | Channel | Slack channel via Socket Mode (bolt). Maps Slack channels/DMs to workspaces. | `[slack] bot_token / app_token` + `uv sync --extra slack` | [Channels](../usage/channels.md) |
| `tui` | Channel | TUI client (Textual). Standalone terminal UI connecting via HTTP/SSE. | Always active | [Channels](../usage/channels.md) |
| `sqlite-memory` | Memory Backend | Persistent per-group memory with BM25-ranked full-text search (save, recall, forget, list). | Always active | [Memory](../usage/memory.md) |
| `caldav` | MCP Server Handler | CalDAV calendar tools (list, create, delete events). Works with Nextcloud and other CalDAV servers. | `[caldav] url / username / password` + `uv sync --extra caldav` | [MCP service tools](../architecture/mcp-service-tools.md) |
| `docker-runtime` | Container Runtime | Docker container runtime. Default on Linux, fallback on macOS. | Always active (requires `docker` CLI) | [Container isolation](../architecture/container-isolation.md) |
| `apple-runtime` | Container Runtime | Apple Container runtime for macOS hosts. | macOS only (auto-detected) | [Container isolation](../architecture/container-isolation.md) |
| `google-setup` | Service Handler + MCP Server | Google Drive and Calendar setup — GCP project creation, API enablement, OAuth authorization. Provides base MCP server specs for `gdrive` and `gcal`. | Always active | [Google Drive](../usage/gdrive.md) |
| `slack-token-extractor` | Service Handler | Extracts fresh Slack browser tokens (xoxc/xoxd) from persistent browser sessions. | Always active | — |
| `x-integration` | Service Handler | Post tweets, like, reply, retweet, and quote on X (Twitter) via browser automation. | Always active | — |
| `notebook-server` | MCP Server | JupyterLab notebook execution server for running Python notebooks in agent containers. | Always active | [Notebooks](../usage/notebooks.md) |
| `sqlite-observer` | Observer | Persists EventBus events to a dedicated `events` table for observability. | Always active | [Observers](../architecture/observers.md) |
| `tailscale` | Tunnel | Tailscale connectivity detection. Warns at startup if tunnel is down. | Always active (requires `tailscale` CLI) | [Tunnels](../architecture/tunnels.md) |

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
