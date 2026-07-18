<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>🦞 Pynchy</em> (pronounced "Pinchy") — A personal AI assistant like <a href="https://github.com/openclaw/openclaw">OpenClaw</a> done right. Security first, modular, written in Python.
</p>


## Why Pynchy?

Everyone is writing their own AI assistant. Why write another one? Mainly because I wanted something written in Python — that's what I'm most comfortable with.

### Comparison to Related Projects

- [ZeroClaw](https://github.com/theonlyhennygod/zeroclaw) looks great actually, but I don't know how to write in Rust.
- [Happy](https://github.com/slopus/happy) looks great, but ultimately is a remote terminal to Claude Code. I want to add my own security features. Also, I am not fluent in TypeScript.
- [NanoClaw](https://github.com/qwibitai/nanoclaw) is too minimalist.
- [OpenClaw](https://github.com/openclaw/openclaw) is a massive pile of overcooked spaghetti code. Ain't no way I'm running that security nightmare on my machine.
- [pi mono](https://github.com/badlogic/pi-mono) is a less crazy project, which OpenClaw built on top of. It doesn't have the security features I want.

## Features

- Agents run in containers, with process, filesystem, and network isolation.
- Built-in plugins ship with the monorepo; third-party plugins are discoverable via Python entry points.
- Uses [LiteLLM](https://docs.litellm.ai/docs/) as the LLM gateway, which gives you a bunch of features out of the box:
  - Automatic load balancing across APIs, to soak up your various allowances from different providers.
  - Access to [100+ LLM providers](https://docs.litellm.ai/docs/providers)
  - Cost tracking and budget management.
  - Rate limiting
  - MCP gateway — manages external MCP tool servers with per-workspace access control and on-demand Docker lifecycle.
  - (see the [LiteLLM docs](https://docs.litellm.ai/docs/) for more details)
- [Ten plugin hook types](docs/plugins/index.md) — agent cores, skills, channels, service handlers, container runtimes, tunnels, observers, memory backends, MCP servers, and workspaces.
- Persistent memory with BM25-ranked full-text search — agents save and recall facts across sessions.
- Recurring tasks scheduled at specific times or intervals.
- Policy groups to prevent [lethal trifecta prompt injection attacks](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/).

## Integrations

Built-in plugins provide integrations with external services, and they're all pluggable — see [plugin authoring](docs/plugins/index.md) to add your own.

| Integration | What it does |
|-------------|-------------|
| **WhatsApp** | Messaging channel via linked device |
| **Slack** | Messaging channel with browser-based token extraction |
| **Discord** | Bot channel for guilds, threads, DMs, and an optional voice workspace |
| **X (Twitter)** | Post, like, reply, retweet, and quote via browser automation |
| **CalDAV** | Calendar access (Nextcloud, etc.) — list, create, delete events |
| **Jupyter Notebooks** | Per-workspace notebook server with MCP tools |
| **Google Drive** | File access via OAuth2 MCP server |

## Getting Started

See the **[installation guide](docs/install.md)**.

## Documentation

Full documentation lives in the [documentation site](docs/index.md).

| Section | What it covers |
|---------|---------------|
| [Usage](docs/usage/index.md) | Day-to-day operation, groups, scheduled tasks |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: channels, skills, MCP servers |
| [Architecture & Design](docs/architecture/index.md) | Container isolation, message routing, IPC, security |
| [Contributing](docs/contributing/contributing-code.md) | How to contribute — plugins, fixes, docs, and more |

## FAQ

**What messaging channels are supported?**
WhatsApp, Slack, Discord, and the local TUI have first-party plugins. Channels are pluggable — write a [plugin](docs/plugins/index.md) to add another one.

**Why Apple Container instead of Docker?**
On macOS, Apple Container is lightweight and optimized for Apple silicon. Docker works too and is used as a fallback. On Linux, Docker is the only option.

**Is this secure?**
Agents run in containers, not behind application-level permission checks. They can only access explicitly mounted directories. See [the security model](docs/architecture/security.md) for details.

**How do I debug issues?**
Ask Pynchy. "Why isn't the scheduler running?" "What's in the recent logs?"

### Credits

Huge thanks to [NanoClaw](https://github.com/qwibitai/nanoclaw). Pynchy started as a Python port of NanoClaw.

## License

MIT
