# Available Plugins

This page tracks plugins that work with pynchy.

## Built-in Plugins

These ship with pynchy and activate when their config section is present:

| Plugin | Purpose | Config |
|--------|---------|--------|
| `slack` | Slack channel via Socket Mode (bolt). Maps Slack channels/DMs to workspaces. | `[slack] bot_token / app_token` |
| `caldav` | CalDAV calendar tools (list, create, delete events). Works with Nextcloud and other CalDAV servers. | `[caldav] url / username / password` |

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
