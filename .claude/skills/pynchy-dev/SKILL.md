---
name: Pynchy Development
description: Use when running pynchy locally — running the app, tests, linting, formatting, prek hooks, or rebuilding the agent container. Also use when determining whether you're on the live Pynchy host or a local machine, and for debugging agent behavior-- session transcript branching, inspecting message history and agent traces in SQLite, pytest hangs, or diagnosing known codebase issues.
---

# Pynchy Development

Run commands directly—don't tell the user to run them.

## Am I on pynchy?

Check `hostname` and compare it with the deployment-specific live host from local memory, environment, or the operator. If you are on that host, access services at `localhost`. Otherwise, reach Pynchy over SSH or Tailscale using that configured host.

## Commands

```bash
uv run pynchy            # Run the app
uv run pytest tests/     # Run tests
uv run ruff check --fix src/  # Lint + autofix
uv run ruff format src/       # Format
uvx prek run --all-files  # Run all repository hooks
./src/pynchy/agent/build.sh     # Rebuild agent container
```

## Managed worktrees and optional runtimes

Use `new-feature create <slug> --no-agent` from the control checkout before any source or
documentation edit. Work in the resulting `.worktrees/<slug>` directory; direct edits on the
target branch are blocked. Setup installs dependencies but does not start a runtime. Start
an isolated Pynchy runtime only when interactive diagnosis needs one.
See [the feature-runtime workflow](../../../docs/contributing/new-feature.md) for create, merge,
restart, and teardown commands. Do not use raw `git worktree` commands for managed features.
Install or verify its host dependencies with `./scripts/install_new_feature_dependencies.py`.

Private overlay edits belong in the independent personalization repository's own managed
worktree, not in the public Pynchy worktree. See
[editing personalization](../../../docs/usage/personalization.md#edit-with-managed-worktrees).

## Documentation Lookup

When you need documentation for a library or framework, use the context7 MCP server to get up-to-date docs. Don't rely on training data for API details that may have changed.

## Testing Philosophy

Write tests that validate **actual business logic**, not just line coverage. See [references/testing-philosophy.md](references/testing-philosophy.md) for what makes a good test vs. coverage theater.

## Known Issues

- **Transcript branching is closed by construction, not to be reopened casually** — native Teams tools (`TeamCreate`/`TeamDelete`/`SendMessage`) are not allow-listed in either core, so nothing can branch the leader's session transcript. `Task` sidechains remain allowed (they write off the main chain and resume safely). Reintroducing Teams requires per-teammate session isolation first — see [session transcript constraints](references/session-transcript.md).

## Debugging Agent Behavior

Prefer querying SQLite over docker logs — docker logs truncate output, but the DB stores full content and captures agent internals (thinking, tool calls, system prompts).

Database: `data/messages.db`. If not on the Pynchy host, prefix commands with `ssh "$PYNCHY_HOST"` after setting `PYNCHY_HOST` to the deployment-specific hostname.

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

If the OpenAI backend shows `/bin/sh: Syntax error: word unexpected (expecting ")")` for shell tool calls, the shell executor is likely receiving a `ShellCommandRequest(...)` object and trying to run its repr. Ensure `_make_shell_executor` in `src/pynchy/agent/agent_runner/src/agent_runner/cores/openai.py` extracts `command` from object/mapping shapes (including parsing repr when needed).

If OpenAI tool calls show up with empty `tool_input` in `events`, the `tool_call_item.raw_item` usually carries the data. Common `raw_item.type` values:
`shell_call` (uses `action.commands`), `local_shell_call` (uses `action.command` list), `apply_patch_call` (uses `operation`), and `function_call`/`mcp_call` (uses JSON `arguments`). Parse those fields before falling back to generic mappings.
