<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>🦞 Pynchy</em> (pronounced "Pinchy") — A personal AI assistant like <a href="https://github.com/openclaw/openclaw">OpenClaw</a> done right. Security first, modular, written in Python.
</p>


## Why Pynchy?

Everyone is writing their own AI assistant. Why write another one? The biggest reason is that I wanted something written in Python, because that's what I'm most comfortable with.

### Comparison to Related Projects

- [ZeroClaw](https://github.com/theonlyhennygod/zeroclaw) looks great actually, but I don't know how to write in Rust.
- [Happy](https://github.com/slopus/happy) looks great, but ultimately is a remote terminal to Claude Code. I want to add my own security features. Also, I am not fluent in TypeScript.
- [NanoClaw](https://github.com/qwibitai/nanoclaw) is a too minimalist.
- [OpenClaw](https://github.com/openclaw/openclaw) is a massive pile of overcooked spaghetti code. Ain't no way I'm running that security nightmare on my machine.
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
- Customizable; [eight types of plugins](https://pynchy.ricardodecal.com/plugins/) are supported — agent cores, skills, channels, service handlers, container runtimes, workspaces, observers, and tunnels.
- Persistent memory with BM25-ranked full-text search — agents save and recall facts across sessions.
- Reoccurring tasks can be scheduled to run at a specific time or interval.
- (work in progress) policy groups to prevent [lethal trifecta prompt injection attacks](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/).

## Integrations

Built-in plugins provide integrations with external services. All integrations are pluggable — see [plugin authoring](https://pynchy.ricardodecal.com/plugins/) to add your own.

| Integration | What it does |
|-------------|-------------|
| **WhatsApp** | Messaging channel via linked device |
| **Slack** | Messaging channel with browser-based token extraction |
| **X (Twitter)** | Post, like, reply, retweet, and quote via browser automation |
| **CalDAV** | Calendar access (Nextcloud, etc.) — list, create, delete events |
| **Jupyter Notebooks** | Per-workspace notebook server with MCP tools |
| **Google Drive** | File access via OAuth2 MCP server |

## Getting Started

See the **[installation guide](https://pynchy.ricardodecal.com/install/)** to get started.

## Documentation

Full documentation at **[pynchy.ricardodecal.com](https://pynchy.ricardodecal.com/)**.

| Section | What it covers |
|---------|---------------|
| [Usage](https://pynchy.ricardodecal.com/usage/) | Day-to-day operation, groups, scheduled tasks |
| [Plugin authoring](https://pynchy.ricardodecal.com/plugins/) | Writing plugins: channels, skills, MCP servers |
| [Architecture & Design](https://pynchy.ricardodecal.com/architecture/) | Container isolation, message routing, IPC, security |
| [Contributing](https://pynchy.ricardodecal.com/contributing/contributing-code/) | How to contribute — plugins, fixes, docs, and more |

## FAQ

**What messaging channels are supported?**
WhatsApp and Slack have first-party plugins. Channels are pluggable — write a [plugin](https://pynchy.ricardodecal.com/plugins/) to add new ones.

**Why Apple Container instead of Docker?**
On macOS, Apple Container is lightweight and optimized for Apple silicon. Docker works too and is used as a fallback. On Linux, Docker is the only option.

**Is this secure?**
Agents run in containers, not behind application-level permission checks. They can only access explicitly mounted directories. See [the security model](https://pynchy.ricardodecal.com/architecture/security/) for details.

**How do I debug issues?**
Ask Pynchy. "Why isn't the scheduler running?" "What's in the recent logs?" That's the AI-native approach.

### Credits

Huge thanks to [NanoClaw](https://github.com/qwibitai/nanoclaw). This project started as a Python port of that project.

## License

MIT
