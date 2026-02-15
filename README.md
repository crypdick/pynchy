<p align="center">
  <img src="assets/mr-pinchy.webp" alt="Pynchy" width="400">
</p>

<p align="center">
  <em>Pynchy</em> — Personal AI assistant with an emphasis on simplicity and security, written in Python.
</p>


## Why This Project Exists

Everyone is writing their own AI assistant. Why write another one? The biggest reason is that I wanted something written in Python, because that's what I'm most comfortable with.

Here is a comparison to related projects:

- [ZeroClaw](https://github.com/theonlyhennygod/zeroclaw) looks great actually, but I don't know how to write in Rust. If I did, I would probably use that instead.
- [NanoClaw](https://github.com/qwibitai/nanoclaw) is a bit too minimalist for my liking.
- [OpenClaw](https://github.com/openclaw/openclaw) is a big inspiration, but it's a monstrosity. It's a security nightmare, and a massive pile of overcooked spaghetti code. Ain't no way I'm running that on my machine.
- [pi mono](https://github.com/badlogic/pi-mono) is a less crazy project, which actually OpenClaw built on top of. It doesn't have the security features that I want.

## Installation


See **[docs/install.md](docs/install.md)**.

## Philosophy

**Secure by isolation.** Agents run in Linux containers (Docker, or Apple Container on macOS). They can only see what's explicitly mounted. Bash access is safe because commands run inside the container, not on your host.

**AI-native.** No installation wizard; Claude Code guides setup. No monitoring dashboard; ask Claude what's happening. No debugging tools; describe the problem, Claude fixes it.

**Modularity.** Contributors shouldn't add features (e.g. support for Telegram) to the codebase. Instead, they contribute [claude code skills](https://code.claude.com/docs/en/skills) or plugins. See **[docs/contributing.md](docs/contributing.md)** for the full contributing guide.

## What It Supports

- **WhatsApp I/O** - Message Claude from your phone
- **Isolated group context** - Each group has its own `CLAUDE.md` memory, isolated filesystem, and runs in its own container sandbox with only that filesystem mounted
- **God channel** - Your private channel (self-chat) for admin control; every other group is completely isolated
- **Scheduled tasks** - Recurring jobs that run Claude and can message you back
- **Web access** - Search and fetch content
- **Container isolation** - Agents sandboxed in Apple Container (macOS) or Docker (macOS/Linux)
- **Agent Swarms** - Spin up teams of specialized agents that collaborate on complex tasks

## Usage

Talk to your assistant with the trigger word (default: `@Pynchy`):

```
@Pynchy send an overview of the sales pipeline every weekday morning at 9am (has access to my Obsidian vault folder)
@Pynchy review the git history for the past week each Friday and update the README if there's drift
@Pynchy every Monday at 8am, compile news on AI developments from Hacker News and TechCrunch and message me a briefing
```

From the God channel (your self-chat), you can manage groups and tasks:
```
@Pynchy list all scheduled tasks across groups
@Pynchy pause the Monday briefing task
@Pynchy join the Family Chat group
```

## Customizing

There are no configuration files to learn. Just tell Claude Code what you want:

- "Change the trigger word to @Bob"
- "Remember in the future to make responses shorter and more direct"
- "Add a custom greeting when I say good morning"
- "Store conversation summaries weekly"

## Requirements

- macOS or Linux
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code](https://claude.ai/download)
- [Apple Container](https://github.com/apple/container) (macOS, preferred) or [Docker](https://docker.com/products/docker-desktop) (macOS/Linux)
- System libraries (libmagic)

See **[docs/install.md](docs/install.md)** for detailed installation instructions and platform-specific dependencies.


## FAQ

**Why WhatsApp and not Telegram/Signal/etc?**

Because I use WhatsApp. Write a new plugin to support new channels.

**Why Apple Container instead of Docker?**

On macOS, Apple Container is the preferred runtime — it's lightweight and optimized for Apple silicon, running Linux containers in minimal VMs via Apple's Virtualization framework. Docker works too and is used as a fallback if Apple Container isn't installed. On Linux, Docker is the only option.

**Is this secure?**

Agents run in containers, not behind application-level permission checks. They can only access explicitly mounted directories. You should still review what you're running. See [docs/security.md](docs/security.md) for the full security model.

**How do I debug issues?**

Ask Claude Code. "Why isn't the scheduler running?" "What's in the recent logs?" "Why did this message not get a response?" That's the AI-native approach.

**Why isn't the setup working for me?**

I don't know. Ask `claude`. If claude finds a bug that is likely affecting other users, open a PR.

**What changes will be accepted into the codebase?**

Security fixes, bug fixes, and clear improvements to the base configuration. That's it.

Everything else (new capabilities, OS compatibility, hardware support, enhancements) should be contributed as plugins.

This keeps the base system minimal and lets every user customize their installation without inheriting features they don't want.

## Credits

Huge thanks to [NanoClaw](https://github.com/qwibitai/nanoclaw). This project started as a Python port of theat project.

## License

MIT
