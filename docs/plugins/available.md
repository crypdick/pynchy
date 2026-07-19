# Available Plugins

This page is the catalog of plugins registered with Pynchy itself. The keys in
the first column are the names used in `[plugins.<key>]` to disable a built-in
plugin. A registered plugin can still require its own channel, tool, or profile
configuration before it is useful.

## Built-in Plugins

Pynchy loads these plugins from its static built-in registry. They are enabled
by default, but you can disable one with `[plugins.<key>] enabled = false`.
Plugins with an optional dependency are skipped when that dependency is not
installed; install the named extra with `uv sync --extra <name>`.

| Key | Type | Purpose | Requirements / configuration | Docs |
|-----|------|---------|------------------------------|------|
| `claude` | Agent core | Claude Agent SDK core | Select with `[agent].default_core = "claude"` | [Agent cores](../usage/agent-cores.md) |
| `claude-cli` | Agent core | Claude Code CLI core | Select with `[agent].default_core = "claude-cli"` | [Agent cores](../usage/agent-cores.md) |
| `openai` | Agent core | OpenAI Agents SDK core | Select with `[agent].default_core = "openai"` | [Agent cores](../usage/agent-cores.md) |
| `codex` | Agent core | OpenAI Codex CLI core | Select with `[agent].default_core = "codex"` | [Agent cores](../usage/agent-cores.md) |
| `discord` | Channel | Discord channel, including text and voice input | `uv sync --extra discord` and `[connections]` configuration | [Channels](../usage/channels.md) |
| `slack` | Channel | Slack Socket Mode channel | `uv sync --extra slack` and `[connections]` configuration | [Channels](../usage/channels.md) |
| `tui` | Channel | Local terminal UI over HTTP/SSE | No external credential | [Channels](../usage/channels.md) |
| `whatsapp` | Channel | WhatsApp channel through Neonize | `uv sync --extra whatsapp` and QR authentication | [Channels](../usage/channels.md) |
| `pocket-tts` | Speech synthesizer | Local neural speech synthesis for spoken replies | Loopback Pocket TTS service | [Local speech synthesis](../usage/local-speech.md) |
| `tailscale` | Tunnel | Tailscale connectivity detection | `tailscale` CLI on the host | [Tunnels](../architecture/tunnels.md) |
| `docker-runtime` | Container runtime | Docker agent-container runtime | Docker CLI and daemon | [Container isolation](../architecture/container-isolation.md) |
| `apple-runtime` | Container runtime | Apple Container agent-container runtime | macOS with Apple Container | [Container isolation](../architecture/container-isolation.md) |
| `caldav` | Service handler | CalDAV calendar actions | `uv sync --extra caldav` and calendar configuration | [MCP service tools](../architecture/mcp-service-tools.md) |
| `slack-token-extractor` | Service handler | Refreshes Slack browser tokens from persistent sessions | Slack browser session | [Slack MCP](../usage/slack-mcp.md) |
| `x-integration` | Service handler | Browser-driven X actions | X tool/profile configuration | [X integration](../usage/x-integration.md) |
| `google` | MCP server specification | Google Drive and Calendar MCP server defaults | Google OAuth configuration | [Google Drive](../usage/gdrive.md) |
| `google-setup` | Service handler | GCP and Google OAuth setup actions | Google Cloud access | [Google Drive](../usage/gdrive.md) |
| `gog` | Service handler | Host-only Gmail, Contacts, Docs, and Sheets actions | Gog CLI and configured host OAuth account | [Google Workspace via Gog](../usage/gog.md) |
| `playwright-browser` | MCP server specification + skill | Browser-control server and usage skill | `uv sync --extra browser` when Playwright is needed | [MCP servers](../usage/mcp.md) |
| `desktop-screenshot` | Service handler | Captures the macOS host desktop | Screen Recording permission | [Desktop screenshots](../usage/desktop-screenshots.md) |
| `computer-use` | Service-handler router + skill | Policy-mediated desktop automation through provider plugins | Enable at least one provider supported by the host | [Computer use](../usage/computer-use.md) |
| `peekaboo` | Computer-use provider | Semantic macOS automation with stable accessibility references | Peekaboo plus macOS Accessibility and Screen Recording permissions | [Computer use](../usage/computer-use.md#built-in-peekaboo) |
| `cua-driver` | Computer-use provider | Compatibility backend for the original macOS action set | Cua Driver plus macOS Accessibility and Screen Recording permissions | [Computer use](../usage/computer-use.md#built-in-cua-driver) |
| `linear` | MCP server specification | Linear issue-tracking tools | `LINEAR_API_KEY` | [Linear](../usage/linear.md) |
| `github` | Webhook route | Direct read-only PR notifications | A public HTTPS endpoint plus one repository-to-workspace route | [GitHub PR notifications](../usage/github.md) |
| `proton-mail` | MCP server specification | Proton Mail tools | Proton Mail Bridge setup | [Proton Mail](../usage/proton-mail.md) |
| `matrix-gateway` | Service handler | Matrix gateway actions | Matrix gateway configuration | [Matrix gateway](../usage/matrix-gateway.md) |
| `notebook` | MCP server specification | Jupyter notebook execution server | `uv sync --extra notebook` | [Notebooks](../usage/notebooks.md) |
| `sqlite-observer` | Observer | Persists operational event summaries | No external dependency | [Observers](../architecture/observers.md) |
| `sqlite-memory` | Memory backend | Per-workspace memory with FTS5 search | No external dependency | [Memory](../usage/memory.md) |

Install every optional extra with `uv sync --extra all` when you want the full
built-in set available.

## Third-Party Plugins

Third-party packages are discovered through Python entry points. Install the
package, configure it if its documentation requires configuration, and restart
Pynchy.

To add a plugin to this catalog:

1. Build it with the [plugin creation guide](quickstart.md).
2. Publish an installable package with a `pynchy` entry point.
3. Open a PR that adds its key, purpose, requirements, and public repository
   link to this page.
