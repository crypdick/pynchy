<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>Pynchy</em> — Personal AI assistant with an emphasis on security and modularity, written in Python.
</p>


## Features

- Agents run in containers, providing process, filesystem, and network isolation.
- Plugins are scanned by an LLM before being installed, providing a basic security audit.
- Customizable; [six types of plugins](docs/plugins/index.md) are supported: FIXME: update with actual number of plugin types
  - LLM Providers
  - MCP Clients/Servers
  - Agents (prompt templates, instructions, and capabilities)
  - Communication channels (Slack, WhatsApp, etc.)
  - Workspaces with isolated memory and Git worktrees.
  - Skills (agent instructions and scripts)
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
