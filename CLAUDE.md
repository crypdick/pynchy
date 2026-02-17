# Pynchy

Personal Claude assistant. See [README.md](README.md) for philosophy. See [docs/install.md](docs/install.md) for installation. See [docs/architecture/](docs/architecture/index.md) for architecture.

## Quick Context

Python process that connects to messaging channels (WhatsApp, Slack, etc. via plugins), routes messages to Claude Agent SDK running in containers (Apple Container on macOS, Docker on Linux). Each group has isolated filesystem and memory.

## Key Files

| File | Purpose |
|------|---------|
| `src/pynchy/db/` | SQLite operations (async, aiosqlite) — package with domain submodules |
| `src/pynchy/ipc/` | IPC watcher, registry-based dispatch, service handlers — package |
| `src/pynchy/git_ops/` | Git sync, worktrees, and shared helpers — package |
| `src/pynchy/chat/` | Message pipeline — channels, commands, routing, output handling |
| `src/pynchy/runtime/` | Runtime detection, platform providers, system checks |
| `src/pynchy/plugin/` | Plugin system, sync, verification, built-in plugins |
| `src/pynchy/container_runner/` | Container orchestration — mounts, credentials, process management |
| `src/pynchy/security/` | Security policy middleware and audit logging |
| `src/pynchy/config.py` | Pydantic BaseSettings config (TOML + env overrides) |
| `src/pynchy/group_queue.py` | Per-group queue with global concurrency limit |
| `src/pynchy/task_scheduler.py` | Runs scheduled tasks |
| `src/pynchy/types.py` | Data models (dataclasses) |
| `src/pynchy/logger.py` | Structured logging (structlog) |
| `groups/{name}/CLAUDE.md` | Per-group memory (isolated) |
| `container/skills/agent-browser.md` | Browser automation tool (available to all agents via Bash) |
| `backlog/TODO.md` | Work item index — one-line items linking to plan files in status folders |

## Detailed Guides

| Guide | When to Read |
|-------|-------------|
| [Architecture](docs/architecture/index.md) | System design, container isolation, message routing, groups, tasks |
| [Security model](docs/architecture/security.md) | Trust model, security boundaries, credential handling |
| [Development & testing](.claude/development.md) | Running commands, writing tests, linting |
| [Deployment](.claude/deployment.md) | Service management, deploy workflow, container builds, GitHub access |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: hooks, packaging, distribution |
| [Plugin security](.claude/plugins.md) | Understanding plugin trust model and sandbox levels |
| [Worktree isolation](.claude/worktrees.md) | How non-god groups get isolated git worktrees |
| [Style guide](.claude/style-guide.md) | Documentation philosophy, information architecture, code comments |

## Python & Tool Usage

- **Always use `uv run python`** instead of bare `python` or `python3`. This ensures the correct virtual environment and dependencies are used.
- **Always use `uvx`** to run Python CLI tools (e.g., `uvx ruff`, `uvx pytest`). Do not install tools globally or use `pip install` for CLI tools.
