# Contributing Code

Contributions welcome. Here's how to get involved.

## Ways to Contribute

### Plugins (the main path)

Pynchy uses a [plugin architecture](../plugins/index.md) with eight hook categories — channels, skills, agent cores, MCP servers, and more. Most new functionality belongs in a plugin, which keeps the core stable and lets users pick exactly what they want.

Writing a plugin makes the highest impact. Some ideas:

- **Channels** — Telegram, Discord, Matrix, IRC, email
- **Skills** — domain-specific agent instructions
- **MCP servers** — new tool integrations
- **Agent cores** — alternative LLM frameworks
- **Workspaces** — specialized task definitions

See the [Plugin Authoring Guide](../plugins/index.md) to create, package, and distribute plugins.

### Core changes

The core codebase accepts:

- Bug fixes
- Security fixes
- Documentation improvements
- Test coverage
- Performance improvements
- Code simplifications and refactoring

For new features, consider whether a plugin would fit better. If unsure, [open an issue](https://github.com/crypdick/pynchy/issues) to discuss the right approach first.

### Documentation

Docs improvements always help. See the [style guide](contributing-docs.md) for conventions.

### Issues and Discussions

Even without writing code, you can help by:

- Reporting bugs
- Suggesting features (many become plugin ideas)
- Answering questions from other users

## Development Setup

See the `pynchy-dev` skill (`.claude/skills/pynchy-dev/SKILL.md`) for running commands, writing tests, and linting.

## Submitting a PR

1. Fork the repo and create a branch from `main`.
2. Make your changes and add tests where applicable.
3. Run the test suite to make sure everything passes.
4. Open a PR with a clear description of what changed and why.

---

**Have an idea but not sure where it fits?** [Open an issue](https://github.com/crypdick/pynchy/issues).
