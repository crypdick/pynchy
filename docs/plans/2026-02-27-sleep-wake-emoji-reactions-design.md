# Sleep/Wake Emoji Reactions

**Date**: 2026-02-27
**Status**: Approved

## Goal

Add visual indicators on Slack when workspaces transition between sleep and wake states:
- `:sunrise:` reaction on the first inbound message that wakes a sleeping workspace
- `:zzz:` reaction on the agent's final text response when it finishes and the workspace goes back to sleep

## Design

### Feature 1: `:sunrise:` on wake

**Trigger**: A real message arrives for a workspace with no active container (`queue.active == False`), routing through the "no active container" branch in `_route_incoming_group()`.

**Behavior**:
- React to `group_messages[0]` (the first new message in the polling batch) with `:sunrise:`
- Only the first message gets the reaction (not subsequent queued messages)
- Reaction stays permanently (historical marker that this message woke the workspace)
- Fires immediately on receipt, before `enqueue_message_check()`

**Changes**:
- `inbound.py:_route_incoming_group()` — add `send_reaction_to_channels()` call before `enqueue_message_check()` at the "no active container" branch (~line 154)

**No Slack channel changes needed** — `"sunrise"` works via the existing `emoji.strip(":")` fallback in `send_reaction()`.

### Feature 2: `:zzz:` on sleep

**Trigger**: Agent finishes processing and the container exits normally (successful run with text output).

**Behavior**:
- React to the agent's final text response (the streamed message) with `:zzz:`
- Only on successful runs that produced visible output
- If the agent errors without sending output, no `:zzz:` (the workspace crashed, not slept)

**Architecture challenge**: Outbound messages have per-channel message IDs (each channel's `post_message()` returns a different ts). The existing `send_reaction_to_channels()` takes a single canonical message ID, designed for inbound messages. Outbound reactions need per-channel dispatch.

**Approach**:
1. **Stash outbound IDs** — In `router.py:_handle_final_result()`, save `stream_state.message_ids` to a module-level `_last_result_ids: dict[str, dict[str, str]]` (chat_jid → {channel_name → raw_ts})
2. **New helper** — `channel_handler.py:send_reaction_to_outbound()` iterates channels, looks up per-channel message IDs, wraps as `slack-{ts}`, and calls `send_reaction()`
3. **Consume after run** — `pipeline.py:process_group_messages()` calls `send_reaction_to_outbound()` after the agent finishes and typing indicator goes off

**Changes**:
- `router.py` — add `_last_result_ids` dict, populate in `_handle_final_result()`, expose `pop_last_result_ids()`
- `channel_handler.py` — add `send_reaction_to_outbound(deps, chat_jid, per_channel_ids, emoji)`
- `pipeline.py` — call the new helper after agent finishes (~line 448)
- Slack `_channel.py` — add `"zzz"` is not needed (works via fallback), but optionally add Unicode `"💤"` → `"zzz"` mapping for consistency

## Emoji Reference

| Emoji | When | Meaning |
|-------|------|---------|
| `:sunrise:` | Message arrives at sleeping workspace | "Waking up..." |
| `:lobster:` | Agent starts reading messages | "Reading your message..." |
| `:crab:` | Follow-up piped to active container | "Forwarded to running agent" |
| `:zzz:` | Agent finishes, container exits | "Going to sleep" |
