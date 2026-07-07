# Discord Channel Follow-ups

Two deferred enhancements to the shipped Discord channel
(`src/pynchy/plugins/channels/discord/`). The v1 channel, streaming, and
parity wiring are done and on `main` (commits `fc66b84`, `6cba9ea`). These are
the remaining items from that feature's follow-up list.

## Context

- Channel is composition-based: `DiscordChannel` root + `DiscordLifecycle` /
  `DiscordAccess` / `DiscordEvents` collaborators + pure `_ids` / `_chunk`.
- v1 assumes a **single Discord connection** (`owns_jid` = any `discord:` jid).
- Iron-law TDD in this repo: no production code without a failing test first.

---

## 1. Interactive AskUser widget (`discord.ui.View`)

Replace the text-only `send_ask_user` (posts the question, user replies in
chat) with an interactive button/select widget, matching Slack's Block Kit
AskUser.

**Current state** (`_channel.py::send_ask_user`): posts
`**Question:**` + a bullet per `q["question"]`, returns `None`. No interaction
handler is registered, and `on_ask_user_answer` is accepted by
`DiscordChannel.__init__` but **not yet wired** through the plugin
(`__init__.py` does not pass `on_ask_user_answer` from
`context.on_ask_user_answer_callback` — Slack/WhatsApp do; add it).

**Contract to satisfy** (mirror Slack `_interactions.py` / `_channel.py`):
- `send_ask_user(jid, request_id, questions: list[dict])` — each question has
  `question` (str), `options` (list of `{label, description}`), `header`,
  `multiSelect` (bool). Returns the posted message id (`discord-<id>`).
- On answer, call `self.on_ask_user_answer(request_id, answer_dict)` where
  `answer_dict = {"answer": <str|list>, "answered_by": <user_id>, ...}`
  (Slack also includes channel/message ids — include the Discord equivalents).

**Design sketch:**
- Build a `discord.ui.View` with one `discord.ui.Button` per option (or a
  `discord.ui.Select` when `multiSelect`), plus a submit affordance for
  multi-select / free text. Embed `request_id` in each component's
  `custom_id` (`ask_user:<request_id>:<option_idx>`), mirroring Slack's
  `action_id` routing.
- Register the interaction handler on the client (`on_interaction` or View
  callbacks). Guard with `DiscordAccess` (same allow rules as messages) so
  only permitted users can answer.
- On submit: assemble `answer_dict`, fire `on_ask_user_answer`, then
  `message.edit` to show the chosen answer and remove interactivity (Slack
  does the same). Handle View timeout (disable components).

**Testing note (why this is the hard one):** `discord.ui` interactions need
faked `Interaction` objects. Keep the answer-assembly + custom_id
parse/build as **pure functions** in a new `_askuser.py` (no `discord`
import), unit-tested exhaustively; keep the View/interaction glue thin, as
`_lifecycle.py` does for the gateway.

---

## 2. DM pairing flow + approve CLI

Let an unknown user DM the bot and get approved without hand-editing config.

**Current state:** `DiscordAccess.decide` already returns the `"pairing"`
decision type (the seam exists); today the allowlist path only returns
`allow` / `deny`. `dm_policy = "allowlist"` denies unknown DMs outright.

**Open design decisions (brainstorm before building):**
- **Where pairing state lives** — sqlite (via `pynchy.state`) vs mutating
  `config.toml`. Leaning sqlite: config stays declarative, pairings are
  runtime data.
- **Approve UX** — a new CLI subcommand (e.g. `pynchy discord approve
  <code>`) vs an admin-channel command vs the env-add-style harness intercept
  already proposed in TODO. Pick one.
- **Pairing-code format & expiry** — short code DM'd to the user; admin
  approves; TTL on pending requests.

**Flow:** unknown DM -> `decide` returns `pairing` -> channel DMs a pairing
code + "ask an admin to approve" -> pending record persisted -> admin approves
-> user id added to the effective allowlist -> subsequent DMs `allow`.

This one is larger and product-shaped; run it through the brainstorming skill
(design doc + approval) before implementing.
