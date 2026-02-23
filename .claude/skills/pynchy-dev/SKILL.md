---
name: Pynchy Development
description: Use when running pynchy locally — running the app, tests, linting, formatting, pre-commit hooks, or rebuilding the agent container. Also use when determining whether you're on pynchy-server or a local machine, and for debugging agent behavior-- session transcript branching, inspecting message history and agent traces in SQLite, pytest hangs, or diagnosing known codebase issues.
---

# Pynchy Development

Run commands directly—don't tell the user to run them.

## Am I on pynchy?

Check `hostname`. If it returns `pynchy-server`, you're on the server and can access services at `localhost`. Otherwise, reach pynchy over Tailscale (e.g., `ssh pynchy-server`).

## Commands

```bash
uv run pynchy            # Run the app
uv run pytest tests/     # Run tests
uv run ruff check --fix src/ container/agent_runner/src/  # Lint + autofix
uv run ruff format src/ container/agent_runner/src/       # Format
uvx pre-commit run --all-files  # Run all pre-commit hooks
./container/build.sh     # Rebuild agent container
```

## Documentation Lookup

When you need documentation for a library or framework, use the context7 MCP server to get up-to-date docs. Don't rely on training data for API details that may have changed.

## Testing Philosophy

Write tests that validate **actual business logic**, not just line coverage. See [references/testing-philosophy.md](references/testing-philosophy.md) for what makes a good test vs. coverage theater.

## Known Issues (2026-02-08)

1. **[FIXED] Resume branches from stale tree position** — subagent CLI processes write to the same session JSONL; on resume, the CLI may pick a stale branch tip. Fix: pass `resumeSessionAt` with the last assistant message UUID.

2. **IDLE_TIMEOUT == CONTAINER_TIMEOUT (both 30 min)** — both timers fire together, so containers exit via hard SIGKILL (code 137) instead of graceful `_close` shutdown. Idle timeout should be shorter (~5 min).

3. **Cursor advanced before agent succeeds** — `processGroupMessages` advances `lastAgentTimestamp` before the agent runs. On timeout, messages are permanently lost.

## Debugging Agent Behavior

Prefer querying SQLite over docker logs — docker logs truncate output, but the DB stores full content and captures agent internals (thinking, tool calls, system prompts).

Database: `data/messages.db`. If not on the pynchy host, prefix with `ssh pynchy-server`.

```bash
# Last 20 messages in a channel
sqlite3 data/messages.db "
  SELECT timestamp, sender_name, message_type, substr(content, 1, 120)
  FROM messages WHERE chat_jid = '<JID>'
  ORDER BY timestamp DESC LIMIT 20;
"

# Last 20 tool calls globally
sqlite3 data/messages.db "
  SELECT timestamp, chat_jid, json_extract(payload, '$.tool_name') AS tool
  FROM events
  WHERE event_type = 'agent_trace'
    AND json_extract(payload, '$.trace_type') = 'tool_use'
  ORDER BY timestamp DESC LIMIT 20;
"
```

- **Full query cookbook** (cross-table traces, thinking, activity timelines): [references/sqlite-queries.md](references/sqlite-queries.md)
- **Session transcript branching**: [references/session-transcript.md](references/session-transcript.md)
- **Pytest hangs (100% pass, never exits)**: [references/pytest-hang-diagnostics.md](references/pytest-hang-diagnostics.md)

## OpenAI Shell Tool Pitfall

If the OpenAI backend shows `/bin/sh: Syntax error: word unexpected (expecting ")")` for shell tool calls, the shell executor is likely receiving a `ShellCommandRequest(...)` object and trying to run its repr. Ensure `_make_shell_executor` in `container/agent_runner/src/agent_runner/cores/openai.py` extracts `command` from object/mapping shapes (including parsing repr when needed).
