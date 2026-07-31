# Discord

Connect Pynchy as a Discord bot for guild channels, threads, and DMs. A thread
becomes an isolated conversation that inherits its parent channel's workspace
configuration.

## Set up Discord

1. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
2. Invite it with the `bot` and `applications.commands` scopes and *View Channels*, *Send Messages*, *Send
   Messages in Threads*, *Read Message History*, *Add Reactions*, *Connect*, and
   *Speak*. Do not grant Administrator.
3. Store the token in the host environment, then reference its name from
   `data/personalization/pynchy.toml`:

   ```bash
   DISCORD_BOT_TOKEN=<bot token>
   ```

4. Configure the connection and its workspaces. Pynchy resolves configured names
   at startup, creates missing text channels when allowed, and keeps raw Discord
   IDs in runtime state:

   ```toml
   [connections.mybot]
   type = "discord"
   bot_token_env = "DISCORD_BOT_TOKEN"
   default_thread_participants = ["<your-discord-user-id>"]
   dm_policy = "allowlist"               # open | allowlist | disabled
   allow_from = ["alice"]
   group_policy = "allowlist"            # open | disabled | allowlist

   [connections.mybot.chat.pynchy]
   name = "Pynchy"
   users = ["alice"]

   [connections.mybot.chat.pynchy.channels.general]
   name = "General"
   kind = "voice"

   [workspaces.discord-general]
   profiles = ["pynchy-dev"]
   chat = "connection.discord.mybot.chat.pynchy.channels.general"

   [workspaces.discord-admin]
   profiles = ["pynchy-dev"]

   [workspaces.discord-dm]
   profiles = ["pynchy-dev"]
   ```

   Repo-backed cores need a profile with `repo = "owner/repo"`. Text-channel
   threads inherit their configured parent workspace profile. Declare persistent
   child threads on the workspace when organizing multiple conversations under
   one profile; see [Organize Child Conversations](../usage/workspaces.md#organize-child-conversations).
   Pynchy adds every `default_thread_participants` user to newly created Discord
   threads; use Discord user snowflakes, not display names.

5. Install dependencies, deploy the change, and inspect the channel state:

   ```bash
   uv sync --locked --inexact --extra discord
   uv run pynchy status
   ```

   `registered_groups` should contain JIDs such as
   `discord:channel:<channel-id>` or `discord:direct:<user-id>`.

## Capabilities

- Guild channels, threads, and DMs
- Inbound and outbound reactions
- Streaming responses with safe 2,000-character splitting
- Interactive `ask_user` prompts with buttons, selects, multi-question modals, and file uploads
- File-upload answers preserve Discord attachment metadata and ephemeral URLs for the waiting agent
- Safe mention defaults that never ping `@everyone` unless asked
- History catch-up after reconnect

## Application commands

Discord exposes Pynchy's host controls as native application commands, so these
controls do not depend on message text or the Message Content Intent:

- `/pause`
- `/reset`
- `/end-session`
- `/redeploy`
- `/pending`
- `/approve short_id` and `/deny short_id`
- `/q message`, `/queue message`, and `/btw message` to queue a follow-up without
  interrupting the current turn

Commands are registered globally when the bot connects. Discord can take time to
propagate global command updates. Configured Discord magic-phrase controls are
not interpreted from message text; explicit `!` direct commands remain available.
Other channels retain their configured text controls.

For the optional voice workspace and inbound audio transcription, see [Voice and
speech](voice-and-speech.md).

---

**Want to customize this?** Write your own channel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
