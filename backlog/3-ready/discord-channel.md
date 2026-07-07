# Discord Channel

Add Discord as a first-class pynchy channel — a `discord.py` bot that reads
messages in guild channels, threads, and DMs, replies, and reacts — at Slack
parity. New channel plugin satisfying the existing `Channel` protocol; no core
changes required.

## Context

- **Template: the Slack channel** (`src/pynchy/plugins/channels/slack/`). Same
  connection model (bot token, gateway, allowlist, rich formatting) and the
  same `Channel` protocol. Follow its **composition** structure (per
  `CONVENTIONS.md` and branch `conventions-fixes` commit `2f762a1`): a channel
  object that owns shared state and implements the outbound protocol,
  constructing plain collaborator objects that each hold a back-reference to
  the channel. Do not use the mixin version still on `main`.
- **Library: `discord.py`** — asyncio-native; handles the gateway, IDENTIFY/
  RESUME, heartbeat, reconnect/backoff, REST rate-limiting, entity cache, and
  partial-message hydration, so this plugin only implements policy + I/O.
  Optional extra in `pyproject.toml`; the plugin returns `None` when no
  `[connection.discord.*]` is configured.
- **v1 scope: Slack parity** — guild channels + threads + DMs, replies,
  reactions, embeds. **DM access = allowlist** (`dm_policy` + `allow_from`); the
  access seam returns `allow`/`deny`/`pairing` so a pairing flow drops in later.
  **AskUser = text-only** (posts the question, user replies in chat); the
  `send_ask_user` seam is preserved for a `discord.ui.View` widget later.

Relevant existing code: `channel_runtime.py` (`pynchy_create_channel` hook +
`ChannelPluginContext`), `types.py` (`Channel`, `OutboundEvent`, `NewMessage`,
`InboundFetchResult`), `formatters/base.py` (`BaseFormatter`, `RenderedMessage`).

## Discord vs Slack: threads

Discord is **not** a flat channel→conversation mapping. A guild `#channel` can
contain many threads, and forum channels are entirely threads. The model:

- **Conversation identity is per-thread.** Each thread has its own channel
  snowflake and becomes its own jid / agent session. Parallel threads under one
  `#topic` are independent conversations — this is the desired behavior.
- **Config/access is inherited from the parent channel.** A thread's snowflake
  is not in config, so `_access` resolves the **parent** channel's config
  (`enabled` / `require_mention` / member allowlist). Allowing a parent channel
  implicitly allows its threads. The parent channel id is carried as message
  metadata for this lookup; it is never part of the jid.

## JID scheme

Flat `discord:<kind>:<snowflake>`, lowercased. Message id: `discord-<messageId>`.
`owns_jid` = `discord:` prefix + allowlist membership.

| entity | jid | notes |
|---|---|---|
| DM | `discord:direct:<userId>` | keyed off the user snowflake (so `allow_from` doubles as DM access), not the DM-channel id |
| guild channel | `discord:channel:<channelId>` | channel snowflake only; guild id is metadata |
| thread | `discord:channel:<threadId>` | thread's own snowflake; parent id is metadata for access lookup |
| group DM (optional) | `discord:group:<channelId>` | |

## Package layout

New package `src/pynchy/plugins/channels/discord/`:

| file | responsibility |
|---|---|
| `__init__.py` | `DiscordChannelPlugin` (`@hookimpl pynchy_create_channel`): read `[connection.discord.*]`, resolve token, build one `DiscordChannel` per connection, return `None` if none |
| `_channel.py` | `DiscordChannel` composition root: shared state + outbound protocol (`send_event`/`post_event`/`update_event`/`send_reaction`/`set_typing`/`send_ask_user`/`fetch_inbound_since`/`owns_jid`); delegates to collaborators |
| `_lifecycle.py` | `DiscordLifecycle(channel)`: `connect`/`disconnect`/`reconnect`/`is_connected`/`prepare_shutdown` over `discord.Client`; run client task; reconnect-on-exit |
| `_access.py` | `DiscordAccess(channel)`: one `decide(message) -> allow \| deny \| pairing`. DM policy → guild allowlist → channel enabled → member/role → `require_mention`. Threads resolve parent-channel config |
| `_events.py` | `DiscordEvents(channel)`: `on_message` + `on_raw_reaction_add/remove`; filter self/bots before queueing; dedup by message id; per-jid async serialization; build `NewMessage`; fire callbacks |
| `_ids.py` | pure helpers: build/parse `discord:<kind>:<snowflake>` |
| `_chunk.py` | pure 2000-char splitter that closes/reopens code fences across boundaries and never splits a UTF-16 surrogate pair |
| `_format.py` | `DiscordFormatter(BaseFormatter)`: `OutboundEvent` → `RenderedMessage(text, metadata={"embeds": [...]})`; plain text by default, embeds for structured events |

Import `TtlCache` from the Slack package for user/channel name caching (promote
to a shared util if cross-package import is awkward).

## Inbound (`_events.py`)

1. Drop the bot's own messages and other bots (`allow_bots` default off)
   **before** queue admission.
2. Dedup by message id (RESUME can redeliver).
3. `DiscordAccess.decide(message)` (threads use parent-channel config).
4. On `allow`: build `NewMessage` (`id=discord-<id>`, `chat_jid`, `sender`,
   `sender_name` via cached lookup, `content`, ISO `timestamp`), report chat
   metadata, enqueue on a per-jid queue for ordering, fire `on_message`.
5. Reactions via `on_raw_reaction_*` (raw → fires on uncached messages); ignore
   self/bots; fire `on_reaction`.

## Outbound (`_channel.py` + `_format.py` + `_chunk.py`)

- `send_event`: render → plain `content`; if `>2000` chars, split via `_chunk.py`
  (embeds attach to the first chunk only).
- Always `allowed_mentions = discord.AllowedMentions.none()` unless a send opts
  in (prevents accidental `@everyone`).
- Suppress link embeds by default (`MessageFlags`); guard against empty sends.
- Map errors `50013` (missing permission) and `50007` (cannot DM) to readable
  logs naming the missing permission.
- `send_reaction` → `message.add_reaction`; `set_typing` → `channel.typing()`;
  `post_event`/`update_event` → send + `message.edit` for streaming.

## Reconnect / catch-up

- Reconnection, heartbeat, and RESUME are `discord.py`'s job; `is_connected()`
  reflects live client state, not a stale flag.
- `fetch_inbound_since(jid, since)`:
  `channel.history(after=discord.utils.time_snowflake(since))`. Snowflakes are
  monotonic, so no Slack-style epsilon hack. Return `InboundFetchResult` with
  the newest message timestamp as the high-water mark.

## Config

```toml
[connection.discord.mybot]
token = "${DISCORD_BOT_TOKEN}"   # env var or literal
application_id = ""              # optional; set if REST app lookup is blocked
enabled = true
dm_policy = "allowlist"          # open | allowlist | disabled
allow_from = ["discord:123..."]  # DM allowlist (user snowflakes); "*" = open
group_policy = "allowlist"       # open | disabled | allowlist

[connection.discord.mybot.chat.myguild]          # guild id or slug
require_mention = true
users = ["discord:123..."]                        # sender allowlist
roles = ["role:456..."]                           # role-id allowlist

[connection.discord.mybot.chat.myguild.channels.general]  # parent channel; covers its threads
enabled = true
require_mention = false
allow = ["some_tool"]            # per-channel tool allow
deny  = ["dangerous_tool"]       # per-channel tool deny (deny wins)
```

One block per bot (no nested `accounts` map). Add config types to `config.py`
mirroring the Slack connection model. Token: config literal or `${ENV}`,
falling back to `DISCORD_BOT_TOKEN` for the default connection; a configured-
but-missing token is reported broken, not silently skipped. ID-only matching
(no name matching in v1).

Gateway intents (Developer Portal + `discord.Intents`): `guilds`,
`guild_messages`, `dm_messages`, `message_content` (privileged),
`guild_reactions`, `dm_reactions`. `members`/`presences` OFF.

## Testing

- Confirm `DiscordChannel` satisfies the `@runtime_checkable` `Channel`
  protocol (`tests/test_channel_protocol.py`).
- Unit tests: `_chunk.py` (fence balancing, surrogate-safe cuts, 2000 boundary),
  `_ids.py` (round-trip incl. thread jids), `_access.decide` (DM policy,
  allowlist, `require_mention`, thread→parent inheritance), `_format.py`.
- Add Discord to `tests/live/test_channel_parity.py` (`parity` marker).
- Mock `discord.Client`/`Message` for inbound/outbound tests (no live gateway).

## Deferred (not in v1)

DM pairing + approve CLI; `discord.ui.View` AskUser widget; voice/audio
messages; auto-threads / forum-channel posting; webhook personas; slash
commands / interactions / components; PluralKit; presence/activity; bot-loop
protection; exec-approvals; markdown-table conversion. (Reading existing
threads is in scope; auto-*creating* threads is not.)

## Build sequence

1. `_ids.py` + `_chunk.py` (pure, unit-tested first).
2. Config types + `__init__.py` hook (returns `None` cleanly).
3. `_format.py`.
4. `DiscordChannel` root + `_lifecycle.py` (connect, `send_event`, `owns_jid`,
   `fetch_inbound_since`).
5. `_access.py` decision tree (incl. thread→parent config resolution).
6. `_events.py` inbound + reactions + per-jid serialization.
7. Parity test + `docs/usage/channels.md` Discord section.

## Done

_(filled in on completion)_
