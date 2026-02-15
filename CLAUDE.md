# Pynchy

Personal Claude assistant. See [README.md](README.md) for philosophy. See [docs/INSTALL.md](docs/INSTALL.md) for installation. See [docs/SPEC.md](docs/SPEC.md) for architecture decisions.

## Quick Context

Python process that connects to WhatsApp, routes messages to Claude Agent SDK running in containers (Apple Container on macOS, Docker on Linux). Each group has isolated filesystem and memory.

## Key Files

| File | Purpose |
|------|---------|
| `src/pynchy/db.py` | SQLite operations (async, aiosqlite) |
| `src/pynchy/ipc.py` | IPC watcher and task processing |
| `src/pynchy/router.py` | Message formatting and outbound routing |
| `src/pynchy/config.py` | Trigger pattern, paths, intervals |
| `src/pynchy/group_queue.py` | Per-group queue with global concurrency limit |
| `src/pynchy/runtime.py` | Container runtime detection (Apple Container / Docker) |
| `src/pynchy/mount_security.py` | Mount path validation and allowlist |
| `src/pynchy/worktree.py` | Git worktree isolation for non-god project_access groups |
| `src/pynchy/task_scheduler.py` | Runs scheduled tasks |
| `src/pynchy/types.py` | Data models (dataclasses) |
| `src/pynchy/logger.py` | Structured logging (structlog) |
| `groups/{name}/CLAUDE.md` | Per-group memory (isolated) |
| `container/skills/agent-browser.md` | Browser automation tool (available to all agents via Bash) |
| `docs/backlog/TODO.md` | Work item index — one-line items linking to plan files in status folders |

## Detailed Guides

| Guide | When to Read |
|-------|-------------|
| [Development & testing](.claude/development.md) | Running commands, writing tests, linting |
| [Deployment](.claude/deployment.md) | Service management, deploy workflow, container builds, GitHub access |
| [Plugin security](.claude/plugins.md) | Understanding plugin trust model and sandbox levels |
| [Worktree isolation](.claude/worktrees.md) | How non-god groups get isolated git worktrees |
| [Style guide](.claude/style-guide.md) | Documentation philosophy, code comment conventions |
