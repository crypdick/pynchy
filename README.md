<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>🦞 Pynchy</em> (pronounced "Pinchy") — A personal AI assistant inspired by <a href="https://github.com/openclaw/openclaw">OpenClaw</a>, with an emphasis on security and modularity, written in Python.
</p>


## Why Pynchy?

Everyone is writing their own AI assistant. Why write another one? The biggest reason is that I wanted something written in Python, because that's what I'm most comfortable with.

### Comparison to Related Projects

- [ZeroClaw](https://github.com/theonlyhennygod/zeroclaw) looks great actually, but I don't know how to write in Rust. If I did, I would probably use that instead.
- [NanoClaw](https://github.com/qwibitai/nanoclaw) is a bit too minimalist for my liking.
- [OpenClaw](https://github.com/openclaw/openclaw) is a big inspiration, but it's a monstrosity. It's a security nightmare, and a massive pile of overcooked spaghetti code. Ain't no way I'm running that on my machine.
- [pi mono](https://github.com/badlogic/pi-mono) is a less crazy project, which actually OpenClaw built on top of. It doesn't have the security features that I want.

## Features

- Agents run in containers, providing process, filesystem, and network isolation.
- Built-in plugins ship with the monorepo; third-party plugins are discoverable via Python entry points.
- Uses [LiteLLM](https://docs.litellm.ai/docs/) as the LLM gateway, providing a bunch of features out of the box:
  - Automatic load balancing across APIs, to soak up your various allowances from different providers.
  - Access to [100+ LLM providers](https://docs.litellm.ai/docs/providers)
  - Cost tracking and budget management.
  - Rate limiting
  - MCP gateway — centralized management of external MCP tool servers with per-workspace access control, on-demand Docker lifecycle, and config-driven setup.
  - (see the [LiteLLM docs](https://docs.litellm.ai/docs/) for more details)
- Customizable; [eight types of plugins](docs/plugins/index.md) are supported — agent cores, skills, channels, service handlers, container runtimes, workspaces, observers, and tunnels.
- Persistent memory with BM25-ranked full-text search — agents save and recall facts across sessions.
- Reoccurring tasks can be scheduled to run at a specific time or interval.
- (work in progress) policy groups to prevent [lethal trifecta prompt injection attacks](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/).

## Integrations

Built-in plugins provide integrations with external services. All integrations are pluggable — see [plugin authoring](docs/plugins/index.md) to add your own.

| Integration | What it does |
|-------------|-------------|
| **WhatsApp** | Messaging channel via linked device |
| **Slack** | Messaging channel with browser-based token extraction |
| **X (Twitter)** | Post, like, reply, retweet, and quote via browser automation |
| **CalDAV** | Calendar access (Nextcloud, etc.) — list, create, delete events |
| **Jupyter Notebooks** | Per-workspace notebook server with MCP tools |
| **Google Drive** | File access via OAuth2 MCP server |

## Getting Started

See **[docs/install.md](docs/install.md)** for installation instructions.

## Documentation

| Section | What it covers |
|---------|---------------|
| [Usage](docs/usage/index.md) | Day-to-day operation, groups, scheduled tasks |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: channels, skills, MCP servers |
| [Architecture & Design](docs/architecture/index.md) | Container isolation, message routing, IPC, security |
| [Contributing](docs/contributing/contributing-code.md) | How to contribute — plugins, fixes, docs, and more |

## FAQ

**What messaging channels are supported?**
WhatsApp and Slack have first-party plugins. Channels are pluggable — write a [plugin](docs/plugins/index.md) to add new ones.

**Why Apple Container instead of Docker?**
On macOS, Apple Container is lightweight and optimized for Apple silicon. Docker works too and is used as a fallback. On Linux, Docker is the only option.

**Is this secure?**
Agents run in containers, not behind application-level permission checks. They can only access explicitly mounted directories. See [the security model](docs/architecture/security.md) for details.

**How do I debug issues?**
Ask Pynchy. "Why isn't the scheduler running?" "What's in the recent logs?" That's the AI-native approach.

### Credits

Huge thanks to [NanoClaw](https://github.com/qwibitai/nanoclaw). This project started as a Python port of that project.

## License

MIT
