# Local Speech Synthesis

Pynchy uses a host-side speech synthesis plugin for spoken channel replies.
The built-in provider is [Pocket TTS](https://github.com/kyutai-labs/pocket-tts),
served only on the local loopback interface. Discord voice is its current
consumer; the provider itself has no dependency on Discord.

## macOS setup

Install the pinned Pocket TTS CLI, create the log directory, and install the
launchd agent. The first service start downloads Pocket TTS's default model.

```bash
uv tool install "pocket-tts==2.1.0"
mkdir -p "$HOME/Library/Logs/pynchy"
sed "s|\$HOME|$HOME|g" launchd/com.pynchy.pocket-tts.plist \
  > "$HOME/Library/LaunchAgents/com.pynchy.pocket-tts.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.pynchy.pocket-tts.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.pynchy.pocket-tts.plist"
```

The template deliberately uses `$HOME` as a placeholder because launchd does
not expand shell variables inside plist values. The `sed` command substitutes
the actual absolute home directory before the agent is installed.

The agent listens on `127.0.0.1:8000`, not on the network. Do not expose that
port through a reverse proxy or firewall rule: it is a local Pynchy dependency,
not a public API.

## Verify and troubleshoot

Check launchd first, then verify an actual WAV synthesis request:

```bash
launchctl print "gui/$(id -u)/com.pynchy.pocket-tts"
curl --fail --form 'text=Pocket TTS is ready.' \
  http://127.0.0.1:8000/tts --output /tmp/pynchy-pocket-tts-check.wav
file /tmp/pynchy-pocket-tts-check.wav
rm /tmp/pynchy-pocket-tts-check.wav
```

Once Pynchy is running, its health endpoint includes the selected provider and
the loopback readiness check:

```bash
uv run pynchy status | jq '.speech'
```

`ready: false` means Pocket TTS did not answer on the loopback endpoint. Check
`$HOME/Library/Logs/pynchy/pocket-tts.err.log` before restarting the launchd
agent. `ready: true` verifies the service boundary; a Discord call still also
requires the Discord voice prerequisites in [Voice and speech](../../channels/voice-and-speech.md).

## Update or reinstall

To apply a new pinned Pocket TTS version, stop the agent, reinstall the CLI,
replace the plist, and start it again:

```bash
launchctl bootout "gui/$(id -u)/com.pynchy.pocket-tts"
uv tool install --reinstall "pocket-tts==2.1.0"
sed "s|\$HOME|$HOME|g" launchd/com.pynchy.pocket-tts.plist \
  > "$HOME/Library/LaunchAgents/com.pynchy.pocket-tts.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.pynchy.pocket-tts.plist"
```

Updating this service does not require changing Discord configuration. Pynchy
will use the provider again as soon as its next synthesis request succeeds.

## Remove

To stop using local speech synthesis, remove the launchd agent and the CLI:

```bash
launchctl bootout "gui/$(id -u)/com.pynchy.pocket-tts"
rm "$HOME/Library/LaunchAgents/com.pynchy.pocket-tts.plist"
uv tool uninstall pocket-tts
```

The built-in Pocket TTS plugin remains registered, but `/status` reports it as
not ready and spoken replies are skipped. Text channels and inbound speech-to-text
continue to work.

To leave the service installed but disable its Pynchy integration, add this to
`data/personalization/pynchy.toml` and restart Pynchy:

```toml
[plugins.pocket-tts]
enabled = false
```
