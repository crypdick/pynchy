# Voice and speech

Use this guide to add a spoken Discord workspace or transcribe incoming audio.

## Discord voice workspace

Pynchy supports one existing voice channel per Discord connection. Configure the
guild and channel by name as shown in [Discord](discord.md), then bind that
channel to a normal workspace. Pynchy never creates a voice channel, and it
keeps the internal ID in runtime state.

Joining the configured channel activates a voice session. Its user and role
allowlists still apply. Pynchy leaves when the last allowed member leaves. Only
final agent replies play as speech; streaming text, tool traces, and status
messages remain silent.

Install the `discord` extra, `ffmpeg`, and a system `libopus` library. Install
and operate Pocket TTS through [Local speech synthesis](../usage/host-capabilities/local-speech.md).
Pynchy reports its readiness in the `speech` section of `/status`.

Pocket TTS listens on `127.0.0.1:8000`. Pynchy encodes its WAV output to Discord
Opus with FFmpeg. If synthesis is unavailable, it logs the failure and does not
fall back to a system voice. Set `PYNCHY_DISCORD_OPUS_LIBRARY` to an absolute
path only when the host installs Opus outside the usual system locations.

DM pairing, interactive question widgets, additional voice rooms, Discord
video, and Stage channels are not supported.

## Inbound speech-to-text

Pynchy can cache supported incoming audio attachments, transcribe them, and
replace the message text before the agent sees the turn. Attachment metadata
records the cached path and transcription status.

The host first uses the `faster-whisper` Python package when installed. If it
is unavailable, it uses `PYNCHY_LOCAL_STT_COMMAND` or a `whisper` executable on
`PATH`. Set `PYNCHY_LOCAL_STT_MODEL` and `PYNCHY_LOCAL_STT_LANGUAGE` to tune the
local defaults. Discord provides the first built-in integration; other channels
need media-download support before they can opt in.

---

**Want to customize this?** Write your own channel or speech plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
