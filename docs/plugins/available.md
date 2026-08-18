# Available Plugins

This page is the catalog of plugins registered with Pynchy itself. The keys in
the first column are the names used in `[plugins.<key>]` to disable a built-in
plugin. A registered plugin can still require its own channel, tool, or profile
configuration before it is useful.

## Built-in Plugins

Pynchy loads these plugins from its static built-in registry. They are enabled
by default, but you can disable one with `[plugins.<key>] enabled = false`.
Plugins with an optional dependency are skipped when that dependency is not
installed; add the named extra without pruning existing packages with
`uv sync --locked --inexact --extra <name>`.

| Key | Type | Purpose | Requirements / configuration | Docs |
|-----|------|---------|------------------------------|------|
| `claude` | Agent core | Claude Agent SDK core | Select with `[agent].default_core = "claude"` | [Agent cores](../usage/agent-cores.md) |
| `claude-cli` | Agent core | Claude Code CLI core | Select with `[agent].default_core = "claude-cli"` | [Agent cores](../usage/agent-cores.md) |
| `openai` | Agent core | OpenAI Agents SDK core | Select with `[agent].default_core = "openai"` | [Agent cores](../usage/agent-cores.md) |
| `codex` | Agent core | OpenAI Codex CLI core | Select with `[agent].default_core = "codex"` | [Agent cores](../usage/agent-cores.md) |
| `discord` | Channel | Discord channel, including text and voice input | `uv sync --locked --inexact --extra discord` and `[connections]` configuration | [Discord](../channels/discord.md) |
| `slack` | Channel | Slack Socket Mode channel | `uv sync --locked --inexact --extra slack` and `[connections]` configuration | [Slack](../channels/slack.md) |
| `whatsapp` | Channel | WhatsApp channel through Neonize | `uv sync --locked --inexact --extra whatsapp` and QR authentication | [WhatsApp](../channels/whatsapp.md) |
| `pocket-tts` | Speech synthesizer | Local neural speech synthesis for spoken replies | Loopback Pocket TTS service | [Local speech synthesis](../usage/host-capabilities/local-speech.md) |
| `tailscale` | Tunnel | Tailscale connectivity detection | `tailscale` CLI on the host | [Tunnels](../architecture/tunnels.md) |
| `docker-runtime` | Container runtime | Docker agent-container runtime | Docker CLI and daemon | [Container isolation](../architecture/container-isolation.md) |
| `apple-runtime` | Container runtime | Apple Container agent-container runtime | macOS with Apple Container | [Container isolation](../architecture/container-isolation.md) |
| `caldav` | Service handler | CalDAV calendar actions | `uv sync --locked --inexact --extra caldav` and calendar configuration | [MCP service tools](../architecture/mcp-service-tools.md) |
| `slack-token-extractor` | Service handler + skill | Refreshes Slack browser tokens from persistent sessions | Slack browser session | [Slack MCP](../integrations/slack-mcp.md) |
| `x-integration` | Service handler + skill | Browser-driven X actions | X tool/profile configuration | [X integration](../integrations/x-integration.md) |
| `google` | MCP server specification | Google Drive and Calendar MCP server defaults | Google OAuth configuration | [Google integrations](../integrations/google/index.md) |
| `google-setup` | Service handler | GCP and Google OAuth setup actions | Google Cloud access | [Google integrations](../integrations/google/index.md) |
| `gog` | Service handler | Host-only Gmail, Contacts, Docs, and Sheets actions | Gog CLI and configured host OAuth account | [Google Workspace via Gog](../integrations/google/workspace-gog.md) |
| `playwright-browser` | MCP server specification + skill | Browser-control server and usage skill | `uv sync --locked --inexact --extra browser` when Playwright is needed | [MCP servers](../usage/mcp.md) |
| `desktop-screenshot` | Service handler | Captures the macOS host desktop | Screen Recording permission | [Desktop screenshots](../usage/host-capabilities/desktop-screenshots.md) |
| `computer-use` | Service handler + skill | Policy-mediated desktop automation through one selected provider plugin | Configure one provider supported by the host | [Computer use](../usage/host-capabilities/computer-use.md) |
| `peekaboo` | Computer-use provider | Semantic macOS automation with stable accessibility references | Peekaboo plus macOS Accessibility and Screen Recording permissions | [Computer use](../usage/host-capabilities/computer-use.md#built-in-peekaboo) |
| `cua-driver` | Computer-use provider | Compatibility backend for the original macOS action set | Cua Driver plus macOS Accessibility and Screen Recording permissions | [Computer use](../usage/host-capabilities/computer-use.md#built-in-cua-driver) |
| `linux-x11` | Computer-use provider | Isolated persistent Linux desktop in Kubernetes | `pynchy-desktop` Deployment, profile claim, and namespace-scoped `pods/exec` | [Computer use](../usage/host-capabilities/computer-use.md#built-in-linux-x11) |
| `ssh-x11` | Computer-use provider | Existing Linux X11 desktop over pinned SSH | SSH credential plus remote `wmctrl`, `import`, and `xdotool` helper | [Computer use](../usage/host-capabilities/computer-use.md#built-in-ssh-x11) |
| `linear` | MCP server specification | Linear issue-tracking tools | `LINEAR_API_KEY` | [Linear](../integrations/linear.md) |
| `github` | Webhook route | Direct read-only PR notifications | A public HTTPS endpoint plus one repository-to-workspace route | [GitHub PR notifications](../integrations/github.md) |
| `proton-mail` | MCP server specification | Proton Mail tools | Proton Mail Bridge setup | [Proton Mail](../integrations/proton-mail.md) |
| `matrix-gateway` | Connection runtime + service handler | Routed Matrix conversations and scoped actions | Named Matrix connection and routes | [Matrix gateway](../integrations/matrix-gateway.md) |
| `marketplace-health` | Service handler | Aggregate marketplace counts and mail-reader health | Host-owned action ledger and Proton Mail tool | [Marketplace health](../integrations/marketplace-health.md) |
| `notebook` | MCP server specification | Jupyter notebook execution server | `uv sync --locked --inexact --extra notebook` | [Notebooks](../integrations/notebooks.md) |
| `sqlite-observer` | Observer | Persists operational event summaries | No external dependency | [Observers](../architecture/observers.md) |

Install every optional extra with `uv sync --locked --inexact --all-extras` when
you want the full built-in set available.

## Third-Party Plugins

Third-party packages are discovered through Python entry points. Install the
package, configure it if its documentation requires configuration, and restart
Pynchy.

To add a plugin to this catalog:

1. Build it with the [plugin creation guide](quickstart.md).
2. Publish an installable package with a `pynchy` entry point.
3. Open a PR that adds its key, purpose, requirements, and public repository
   link to this page.
