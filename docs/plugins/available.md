# Available Plugins

This page tracks plugins that work with pynchy.

## Built-in Plugins

These ship with pynchy and are always available. Some activate only when their config section is present:

| Plugin | Type | Purpose | Config |
|--------|------|---------|--------|
| `agent_claude` | Agent Core | Default Claude SDK agent core. | Always active |
| `agent_openai` | Agent Core | OpenAI Agents SDK alternative. | `PYNCHY_AGENT_CORE=openai` |
| `slack` | Channel | Slack channel via Socket Mode (bolt). Maps Slack channels/DMs to workspaces. | `[slack] bot_token / app_token` |
| `caldav` | MCP Server Handler | CalDAV calendar tools (list, create, delete events). Works with Nextcloud and other CalDAV servers. | `[caldav] url / username / password` |
| `tailscale` | Tunnel | Tailscale connectivity detection. Warns at startup if tunnel is down. | Always active (requires `tailscale` CLI) |

## First-Party Plugins

These are maintained by the pynchy project and installed via `[plugins.*]` in config.toml:

| Plugin | Purpose | Repository |
|--------|---------|------------|
| `whatsapp` | Adds the WhatsApp channel integration. | [crypdick/pynchy-plugin-whatsapp](https://github.com/crypdick/pynchy-plugin-whatsapp) |
| `apple` | Adds Apple Container runtime support for macOS hosts. | [crypdick/pynchy-plugin-apple-runtime](https://github.com/crypdick/pynchy-plugin-apple-runtime) |
| `code-improver` | Provides the periodic code-improver workspace for the `pynchy` core repository. | [crypdick/pynchy-plugin-code-improver](https://github.com/crypdick/pynchy-plugin-code-improver) |

## Community-Contributed Plugins

Community plugins are welcome.

To add your plugin to this registry:

1. Build your plugin using the [plugin creation guide](quickstart.md).
2. Open a PR that updates this page with your plugin entry.

Include:

- Plugin name (entry-point key)
- Short description
- Public repository URL
