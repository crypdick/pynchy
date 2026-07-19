# Channel and speech hooks

## `pynchy_create_channel`

Provide a communication channel. Pynchy collects non-`None` channels and host
configuration selects the active connections.

```python
@hookimpl
def pynchy_create_channel(self, context: ChannelPluginContext) -> Channel | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return TelegramChannel(bot_token=token, on_message=context.on_message_callback)
```

`ChannelPluginContext` supplies callbacks for inbound messages, chat metadata,
outbound text, reactions, structured answers, approval decisions, and workspace
lookups. Return a `Channel` that implements `connect`, `send_event`,
`is_connected`, `owns_jid`, `disconnect`, `reconnect`, and `prepare_shutdown`.

Channel plugins run persistently in the host process with filesystem and network
access. Treat them as the highest-risk plugin category; see [Security
architecture](../../architecture/security.md).

## `pynchy_speech_synthesizer`

Provide a host-side synthesizer for final spoken replies:

```python
@hookimpl
def pynchy_speech_synthesizer(self) -> SpeechSynthesizer | None:
    return MySpeechSynthesizer()
```

Pynchy collects valid providers and uses the first. The provider exposes `name`,
`async synthesize(text, output_path)`, and `async health()`. Channels receive the
selected provider in `ChannelPluginContext.speech_synthesizer` and must tolerate
`None` instead of choosing their own fallback. See [Voice and speech](../../channels/voice-and-speech.md).
