# Channel Plugins

## Overview

Enable new communication platforms (Telegram, Slack, Discord, etc.) to be added as plugins. WhatsApp remains built-in.

## Dependencies

- Plugin discovery system (plugin-discovery.md)

## Design

### ChannelPlugin Class

```python
class ChannelPlugin(PluginBase):
    """Base class for channel plugins."""

    categories = ["channel"]  # Fixed

    @abstractmethod
    def create_channel(self, ctx: PluginContext) -> Channel:
        """Return a Channel instance that will be connected on startup.

        Args:
            ctx: Context object providing access to host services
        """
        ...

    def requires_credentials(self) -> list[str]:
        """Return list of required environment variables.

        Optional hook for validation. Checked during startup.
        Example: ["TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID"]
        """
        return []
```

### PluginContext

```python
@dataclass
class PluginContext:
    """Context passed to plugins during initialization."""

    registered_groups: Callable[[], dict[str, RegisteredGroup]]
    send_message: Callable[[str, str], Awaitable[None]]
    # Add more as needed
```

### Channel Protocol (existing)

Plugins must return an object implementing the `Channel` protocol from `types.py`:

```python
class Channel(Protocol):
    name: str
    prefix_assistant_name: bool

    async def connect(self) -> None: ...
    async def send_message(self, jid: str, text: str) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
```

## Example: Telegram Plugin

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-telegram"
dependencies = ["pynchy", "python-telegram-bot"]

[project.entry-points."pynchy.plugins"]
telegram = "pynchy_plugin_telegram:TelegramPlugin"
```

**plugin.py:**
```python
import os
from pynchy.plugin import ChannelPlugin, PluginContext
from pynchy.types import Channel
from .channel import TelegramChannel

class TelegramPlugin(ChannelPlugin):
    name = "telegram"
    version = "0.1.0"
    description = "Telegram messaging integration"

    def create_channel(self, ctx: PluginContext) -> Channel:
        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        return TelegramChannel(
            bot_token=bot_token,
            on_message=ctx.on_message_callback,
            registered_groups=ctx.registered_groups,
        )

    def requires_credentials(self) -> list[str]:
        return ["TELEGRAM_BOT_TOKEN"]
```

**channel.py:**
Contains `TelegramChannel` class implementing the `Channel` protocol.

## Implementation Steps

1. Define `ChannelPlugin` base class in `plugin/channel.py`
2. Define `PluginContext` dataclass
3. Update `app.py:run()`:
   - Create PluginContext after loading state
   - For each channel plugin: `channel = plugin.create_channel(ctx)`
   - Append to `self.channels`
   - Connect alongside WhatsApp
4. Add credential validation during startup
5. Tests: plugin instantiation, channel lifecycle, multi-channel messaging

## Integration Points

- `app.py:run()` — discovers plugins, creates channels, connects them
- `app.py:_on_inbound()` — handles messages from all channels
- `app.py:_broadcast_*()` — sends to all connected channels
- Message routing via `_find_channel()` already supports multiple channels

## Open Questions

- Should channels be hot-reloadable without restart?
- How to handle channel-specific message formatting requirements?
- Do we need per-channel rate limiting configuration?
- Should plugin validation happen at discovery or connection time?
- How to handle channels that need async initialization?

## Multi-Channel Considerations

The existing code already supports multiple channels:

- `self.channels` is a list
- Broadcasting loops over all channels
- Each channel has `owns_jid()` for routing

Plugins just add new items to this list.

## Verification

1. Create test plugin: `pynchy-plugin-test-channel`
2. Install: `uv pip install -e /tmp/pynchy-plugin-test-channel`
3. Verify channel appears in startup logs
4. Send message, verify it reaches the plugin's channel
5. Uninstall and verify clean removal
