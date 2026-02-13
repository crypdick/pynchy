# System Message Logging

Log actual LLM system messages (the system prompt role) to the database, now that "host" messages have their own distinct sender.

## Context

We just renamed pynchy's internal "system" messages to "host" messages (`sender='host'`, `[host]` prefix, `<host>` tags). This frees up the "system" namespace for its standard LLM meaning: the system prompt that shapes the agent's behavior.

Currently, system prompts are not logged anywhere persistent. Logging them would enable:
- Debugging agent behavior by reviewing what system prompt it received
- Auditing prompt changes over time
- Correlating agent outputs with the instructions it was given

## Plan

### 1. Add `sender='system'` to the message schema vocabulary

No schema changes needed — the `sender` column is freeform TEXT. Just start writing rows with `sender='system'`.

### 2. Log the system prompt when launching a container agent

In `container_runner.py` (or `app.py._run_agent`), after assembling the system prompt, store it:

```python
await store_message_direct(
    id=f"system-{int(datetime.now(UTC).timestamp() * 1000)}",
    chat_jid=chat_jid,
    sender="system",
    sender_name="system",
    content=system_prompt_text,
    timestamp=ts,
    is_from_me=True,
)
```

### 3. Ensure system messages stay invisible to agents and chat history

The existing SQL filters already handle this correctly:
- `get_new_messages` / `get_messages_since`: filter `sender != 'host'` — system messages pass through, but they're stored with `is_from_me=True` and bot prefix, so they won't trigger agent runs
- `get_chat_history` / `_EXCLUDE_INTERNAL_HOST`: only hides `sender='host'` messages without `[host]` prefix — system messages would show in history

**Decision needed**: Should system prompts appear in chat history (TUI/API)? Options:
- a) Hide them entirely (add `AND sender != 'system'` to `_EXCLUDE_INTERNAL_HOST`)
- b) Show them in TUI but collapsed/dimmed
- c) Show them only in a dedicated "debug" view

### 4. Update the deploy continuation comment

Line in `app.py` currently says `sender="host" is excluded`. If system messages should also be excluded from triggering agents, add them to the filter.

## Open Questions

- Where exactly is the system prompt assembled? Need to trace `CLAUDE.md` loading + any dynamic context injection to find the right interception point.
- Should scheduled task system prompts also be logged?
- Storage concern: system prompts can be large (several KB). With frequent agent runs, this could bloat the DB. Consider: only log on session creation, not every turn? Or store a hash and only log when it changes?
