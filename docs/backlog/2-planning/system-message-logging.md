# System Message Logging

Log actual LLM system messages (the system prompt role) to the database, now that "host" messages have their own distinct sender.

## Design Principle

**Transparent token stream**: the chat history should be a faithful representation of the LLM context. A user reading the conversation should be able to reconstruct exactly what the model saw — system prompts, user messages, assistant responses, tool calls, and host notifications. Nothing hidden.

Documented in `docs/REQUIREMENTS.md` under "Transparent Token Stream".

## Context

We renamed pynchy's internal "system" messages to "host" messages (`sender='host'`, `[host]` prefix, `<host>` tags). This frees up the "system" namespace for its standard LLM meaning: the system prompt that shapes the agent's behavior.

Currently, system prompts are not logged anywhere persistent. Logging them would enable:
- Debugging agent behavior by reviewing what system prompt it received
- Auditing prompt changes over time
- Correlating agent outputs with the instructions it was given

## Decisions (resolved)

- **Always log** system prompts — every agent run, including scheduled tasks
- **Always show** system messages in chat history (TUI, API, WhatsApp history)
- Storage bloat is acceptable — system prompts are a few KB, and the transparency is worth it

## Plan

### 1. Add `sender='system'` to the message schema vocabulary

No schema changes needed — the `sender` column is freeform TEXT. Just start writing rows with `sender='system'`.

### 2. Find the system prompt assembly point

Trace where the system prompt (CLAUDE.md contents + dynamic context) is assembled before being passed to the container agent. This is the interception point for logging.

Likely in `container_runner.py` where the `ContainerInput` is built, or wherever the CLAUDE.md files are read and concatenated.

### 3. Log the system prompt when launching a container agent

At the interception point, store the full system prompt:

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

Do this for both regular message processing (`_process_group_messages`) and scheduled task runs.

### 4. Ensure system messages don't trigger agent runs

The SQL filters use `sender != 'host'` to exclude host messages from agent-triggering queries. System messages must also be excluded:

```sql
AND sender != 'host' AND sender != 'system'
```

Update in `get_new_messages()` and `get_messages_since()` in `db.py`.

### 5. Keep system messages visible in chat history

The `_EXCLUDE_INTERNAL_HOST` filter only hides `sender='host'` rows without `[host]` prefix. System messages (`sender='system'`) will naturally pass through `get_chat_history()` — no changes needed there.

### 6. Verify the full sender vocabulary is documented

After this change, the sender values are:
- `system` — LLM system prompt (the instructions the model receives)
- `host` — pynchy process notifications (boot, deploy, errors)
- `bot` — assistant responses
- `deploy` — deploy continuation markers
- `tui-user` — messages from the TUI client
- `{phone_jid}` — WhatsApp user messages (sender is the user's JID)
