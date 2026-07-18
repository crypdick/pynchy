# Discord General Voice Workspace

Bind Pynchy to the existing Discord `General` voice channel so an allowed member can talk to one configured workspace from the Discord mobile app.

## Decisions

- This feature uses Discord voice, not PSTN calling, ringing, voicemail, or phone numbers.
- `General` remains an ordinary top-level Discord voice channel. Pynchy does not create channels, use threads, or model a parent room hierarchy.
- One Discord connection may configure one Pynchy voice channel. This respects Discord's single active bot voice connection per guild and keeps the workspace boundary unambiguous.
- Human configuration uses guild and channel names. Pynchy resolves and stores the Discord snowflake internally as `discord:voice:<id>`.
- The voice binding belongs to a normal Pynchy workspace, which selects any desired profile set. Its queue, session history, security policy, and agent configuration remain isolated from every other workspace.

## Requirements

- A Discord channel configuration can declare `kind = "voice"` and name the existing `General` channel.
- A workspace can bind to that named Discord target with `chat = "connection.discord.<connection>.chat.<guild>.channels.<channel>"` and select profiles normally.
- Startup resolves the configured channel by name and registers its hidden voice JID without creating a text or voice channel.
- Pynchy joins when an allowed human member enters the configured room and leaves when no allowed members remain.
- Entering the configured room acts as the voice activation gesture. Discord guild/channel user and role allowlists still apply; bot and unauthorized members cannot start a session.
- Incoming RTP audio uses Discord's DAVE encryption, becomes bounded speech turns, passes through host STT, and enters the existing workspace message queue. Raw turn files are discarded after transcription.
- Only the agent's final response is synthesized and played back. Stream fragments, trace output, and tool chatter stay silent.
- Local STT, local TTS, `libopus`, and `ffmpeg` remain host operator prerequisites and must fail clearly when absent.

## Configuration shape

```toml
[connections.synapse.chat.pynchy]
name = "Pynchy"
users = ["allowed-member"]

[connections.synapse.chat.pynchy.channels.general]
name = "General"
kind = "voice"

[workspaces.general]
profiles = ["pynchy"]
chat = "connection.discord.synapse.chat.pynchy.channels.general"
```

## Non-goals

- Multiple on-demand voice workspaces, channel creation, deletion, or approval-gated provisioning.
- Text-to-speech in DMs, text channels, video, Stage channels, or Discord threads.
- Retaining raw live voice audio after transcription.

## Acceptance

- The configured `General` name resolves to one hidden `discord:voice:<id>` workspace binding.
- An allowed user joins from Discord mobile, speaks a turn, and receives only that workspace's final reply as speech.
- An unauthorized member, unconfigured guild/channel, missing STT/TTS provider, missing `libopus`, or unavailable `ffmpeg` fails safely without creating a new room or bypassing workspace access policy.
