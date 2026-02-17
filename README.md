<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>🦞 Pynchy</em> (pronounced "Pinchy") — A personal AI assistant inspired by [OpenClaw](https://github.com/openclaw/openclaw), with an emphasis on security and modularity, written in Python.
</p>


## Features

- Agents run in containers, providing process, filesystem, and network isolation.
- Built-in plugins ship with the monorepo; third-party plugins are discoverable via Python entry points.
- Uses [LiteLLM](https://docs.litellm.ai/docs/) as the LLM gateway, providing a bunch of features out of the box:
  - Automatic load balancing across APIs, to soak up your various allowances from different providers.
  - Access to [100+ LLM providers](https://docs.litellm.ai/docs/providers)
  - Cost tracking and budget management.
  - Rate limiting
  - (see the [LiteLLM docs](https://docs.litellm.ai/docs/) for more details)
- Customizable; [five types of plugins](docs/plugins/index.md) are supported:
  - LLM Providers
  - MCP Clients/Servers
  - Agents (prompt templates, instructions, and capabilities)
  - Communication channels (Slack, WhatsApp, etc.)
  - Workspaces with isolated memory and Git worktrees.
- Persistent memory with BM25-ranked full-text search — agents save and recall facts across sessions.
- Reoccurring tasks can be scheduled to run at a specific time or interval.
- (work in progress) policy groups to prevent [lethal trifecta prompt injection attacks](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/).

## Getting Started

See **[docs/install.md](docs/install.md)** for installation instructions.

## Documentation

| Section | What it covers |
|---------|---------------|
| [Why Pynchy?](docs/why-pynchy.md) | Motivation and comparison to related projects |
| [Usage](docs/usage/index.md) | Day-to-day operation, groups, scheduled tasks |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: channels, skills, MCP servers |
| [Architecture & Design](docs/architecture/index.md) | Container isolation, message routing, IPC, security |
| [Contributing](docs/contributing.md) | What changes are accepted, plugin-first philosophy |

## FAQ

**What messaging channels are supported?**
WhatsApp and Slack have first-party plugins. Channels are pluggable — write a [plugin](docs/plugins/index.md) to add new ones.

**Why Apple Container instead of Docker?**
On macOS, Apple Container is lightweight and optimized for Apple silicon. Docker works too and is used as a fallback. On Linux, Docker is the only option.

**Is this secure?**
Agents run in containers, not behind application-level permission checks. They can only access explicitly mounted directories. See [the security model](docs/architecture/security.md) for details.

**How do I debug issues?**
Ask Pynchy. "Why isn't the scheduler running?" "What's in the recent logs?" That's the AI-native approach.

## License

MIT
