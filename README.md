<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>Pynchy</em> — Personal AI assistant with an emphasis on simplicity and security, written in Python.
</p>


## Philosophy

**Secure by isolation.** Agents run in Linux containers (Docker, or Apple Container on macOS). They can only see what's explicitly mounted. Bash access is safe because commands run inside the container, not on your host.

**AI-native.** No installation wizard; Pynchy guides setup. No monitoring dashboard; ask Pynchy what's happening. No debugging tools; describe the problem, Pynchy fixes it.

**Modularity.** New capabilities are added as plugins, not features in the base codebase. Plugins can provide:

- **Channels** — Communication platforms (WhatsApp, Slack, Telegram, Discord)
- **Agent Cores** — Alternative LLM engines (OpenAI, Ollama)
- **MCP Servers** — Tools for agents via Model Context Protocol
- **Skills** — Agent instructions and capabilities (markdown)
- **Workspaces** — Managed workspace and task definitions

See **[docs/plugins/index.md](docs/plugins/index.md)** for the plugin authoring guide.

## Getting Started

See **[docs/install.md](docs/install.md)** for installation instructions.

## Documentation

| Section | What it covers |
|---------|---------------|
| [Usage](docs/usage/index.md) | Day-to-day operation, groups, scheduled tasks |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: channels, skills, MCP servers |
| [Architecture](docs/architecture/index.md) | Container isolation, message routing, IPC, security |
| [Contributing](docs/contributing.md) | What changes are accepted, plugin-first philosophy |
| [Why Pynchy?](docs/why-pynchy.md) | Motivation and comparison to related projects |

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
