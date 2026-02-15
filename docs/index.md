# Pynchy

**Personal Claude assistant that runs securely in containers.**

<p align="center">
  <img src="../assets/pynchy.png" alt="Pynchy" width="400">
</p>

Pynchy gives you a personal AI assistant in a codebase you can understand in 8 minutes. One process. A handful of files. Agents run in actual Linux containers with filesystem isolation, not behind permission checks.

## Quick Start

```bash
git clone https://github.com/crypdick/pynchy.git
cd pynchy
uv sync
./container/build.sh
uv run pynchy auth    # scan QR code with WhatsApp
uv run pynchy         # start
```

See [Installation](INSTALL.md) for the full guide including headless server deployment.

## What It Supports

- **WhatsApp I/O** — Message Claude from your phone
- **Isolated group context** — Each group has its own `CLAUDE.md` memory, isolated filesystem, and container sandbox
- **God channel** — Your private channel (self-chat) for admin control
- **Scheduled tasks** — Recurring jobs that run Claude and can message you back
- **Web access** — Search and fetch content
- **Container isolation** — Agents sandboxed in Apple Container (macOS) or Docker (macOS/Linux)
- **Agent Swarms** — Spin up teams of specialized agents that collaborate on complex tasks

## Usage

Talk to your assistant with the trigger word (default: `@Pynchy`):

```
@Pynchy send an overview of the sales pipeline every weekday morning at 9am
@Pynchy review the git history for the past week each Friday and update the README if there's drift
@Pynchy every Monday at 8am, compile news on AI developments from Hacker News and TechCrunch
```

From the God channel (your self-chat), you can manage groups and tasks:

```
@Pynchy list all scheduled tasks across groups
@Pynchy pause the Monday briefing task
@Pynchy join the Family Chat group
```

## Philosophy

- **Small enough to understand** — One Python process, a few source files
- **Secure by isolation** — OS-level container isolation, not permission checks
- **Built for one user** — Fork it, customize the code
- **AI-native** — Claude Code guides setup, debugging, and customization
- **Plugins over features** — Contribute plugins, not features

## Architecture

```
WhatsApp (neonize) → SQLite → Polling loop → Container (Claude Agent SDK) → Response
```

See [Architecture Overview](SPEC.md) for detailed design decisions and [Message Types](architecture/message-types.md) for the message system.
