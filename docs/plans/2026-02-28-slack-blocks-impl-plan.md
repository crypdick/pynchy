# Slack Blocks Formatter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the text-only outbound pipeline with an event-based architecture where channels own rendering via composable formatters, then build a Slack Block Kit renderer with interactive features.

**Architecture:** Pipeline produces `OutboundEvent` objects, channels implement `send_event(jid, event)` which calls `self.formatter.render(event)` then sends via internal transport. `TextFormatter` captures existing logic; `SlackBlocksFormatter` renders to Block Kit.

**Tech Stack:** Python dataclasses, ABC, Slack Block Kit (`markdown`, `rich_text`, `context`, `context_actions` blocks), slack_bolt

**Design doc:** `docs/plans/2026-02-28-slack-blocks-formatter-design.md`

---

### Task 1: OutboundEvent types + BaseFormatter + RenderedMessage

Foundation types. Pure additions — no existing code breaks.

**Files:**
- Modify: `src/pynchy/types.py` (add after line 281, after `ContainerOutput`)
- Create: `src/pynchy/host/orchestrator/messaging/formatters/__init__.py`
- Create: `src/pynchy/host/orchestrator/messaging/formatters/base.py`
- Test: `tests/test_formatters_base.py`

**Step 1: Write the failing test**

```python
# tests/test_formatters_base.py
from pynchy.types import OutboundEvent, OutboundEventType
from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter, RenderedMessage


def test_outbound_event_creation():
    event = OutboundEvent(
        type=OutboundEventType.TOOL_TRACE,
        content="running command",
        metadata={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert event.type == OutboundEventType.TOOL_TRACE
    assert event.content == "running command"
    assert event.metadata["tool_name"] == "Bash"


def test_outbound_event_defaults():
    event = OutboundEvent(type=OutboundEventType.TEXT, content="hello")
    assert event.metadata == {}


def test_rendered_message_defaults():
    msg = RenderedMessage(text="hello")
    assert msg.blocks is None
    assert msg.metadata == {}


def test_rendered_message_with_blocks():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    msg = RenderedMessage(text="hi", blocks=blocks)
    assert msg.blocks == blocks


def test_base_formatter_is_abstract():
    import pytest
    with pytest.raises(TypeError):
        BaseFormatter()  # type: ignore[abstract]
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_formatters_base.py -v`
Expected: FAIL (imports not found)

**Step 3: Write implementation**

Add to `src/pynchy/types.py` after line 281 (after `ContainerOutput`):

```python
from enum import Enum

class OutboundEventType(Enum):
    TEXT = "text"
    TOOL_TRACE = "tool_trace"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    SYSTEM = "system"
    HOST = "host"
    RESULT = "result"


@dataclass
class OutboundEvent:
    type: OutboundEventType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

Note: `types.py` already imports `from __future__ import annotations`, `dataclass`, `field`. Add `Enum` to imports and `Any` from typing.

Create `src/pynchy/host/orchestrator/messaging/formatters/base.py`:

```python
"""Base formatter protocol and rendered message type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pynchy.types import OutboundEvent


@dataclass
class RenderedMessage:
    """Output of a formatter — what gets sent to the channel transport."""

    text: str
    blocks: list[dict] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseFormatter(ABC):
    """Abstract base for channel message formatters."""

    @abstractmethod
    def render(self, event: OutboundEvent) -> RenderedMessage:
        """Convert an outbound event into a channel-ready message."""
        ...

    @abstractmethod
    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage:
        """Render multiple events as a single message (for trace batching)."""
        ...
```

Create `src/pynchy/host/orchestrator/messaging/formatters/__init__.py`:

```python
"""Formatter protocol and implementations."""

from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter, RenderedMessage

__all__ = ["BaseFormatter", "RenderedMessage"]
```

**Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_formatters_base.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/types.py src/pynchy/host/orchestrator/messaging/formatters/ tests/test_formatters_base.py
git commit -m "feat: add OutboundEvent types and BaseFormatter protocol"
```

---

### Task 2: TextFormatter

Extract existing `formatter.py` rendering logic into `TextFormatter`. The key functions to wrap: `format_internal_tags`, `format_tool_preview`, `format_outbound`, `_truncate_output` (from router.py:80-85).

**Files:**
- Create: `src/pynchy/host/orchestrator/messaging/formatters/text.py`
- Modify: `src/pynchy/host/orchestrator/messaging/formatters/__init__.py` (re-export)
- Test: `tests/test_formatters_text.py`

**Step 1: Write the failing test**

```python
# tests/test_formatters_text.py
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.types import OutboundEvent, OutboundEventType


def test_render_tool_trace_bash():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TOOL_TRACE,
        content="",
        metadata={"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
    )
    result = fmt.render(event)
    assert "🔧" in result.text
    assert "ls -la" in result.text
    assert result.blocks is None


def test_render_result_with_prefix():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Here is the answer",
        metadata={"prefix_assistant_name": True},
    )
    result = fmt.render(event)
    assert result.text.startswith("🦞 ")
    assert "Here is the answer" in result.text


def test_render_result_no_prefix():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Here is the answer",
        metadata={"prefix_assistant_name": False},
    )
    result = fmt.render(event)
    assert not result.text.startswith("🦞")


def test_render_text_with_cursor():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="streaming text",
        metadata={"cursor": True},
    )
    result = fmt.render(event)
    assert result.text.endswith(" ▌")


def test_render_text_no_cursor():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="final text",
        metadata={"cursor": False},
    )
    result = fmt.render(event)
    assert "▌" not in result.text


def test_render_thinking():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.THINKING, content="analyzing code")
    result = fmt.render(event)
    assert "💭" in result.text
    assert "analyzing code" in result.text


def test_render_tool_result():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.TOOL_RESULT, content="file contents here")
    result = fmt.render(event)
    assert "📋" in result.text


def test_render_tool_result_verbose():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TOOL_RESULT,
        content="plan text here",
        metadata={"verbose": True, "tool_name": "ExitPlanMode"},
    )
    result = fmt.render(event)
    assert "ExitPlanMode" in result.text
    assert "plan text here" in result.text


def test_render_system():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.SYSTEM, content="system: init")
    result = fmt.render(event)
    assert "⚙️" in result.text


def test_render_host():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.HOST, content="deployment started")
    result = fmt.render(event)
    assert "🏠" in result.text


def test_render_internal_tags():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello <internal>thinking about it</internal> world",
        metadata={"prefix_assistant_name": True},
    )
    result = fmt.render(event)
    assert "<internal>" not in result.text
    assert "🧠" in result.text
    assert "world" in result.text


def test_render_batch():
    fmt = TextFormatter()
    events = [
        OutboundEvent(type=OutboundEventType.THINKING, content="hmm"),
        OutboundEvent(
            type=OutboundEventType.TOOL_TRACE,
            content="",
            metadata={"tool_name": "Bash", "tool_input": {"command": "pwd"}},
        ),
    ]
    result = fmt.render_batch(events)
    assert "💭" in result.text
    assert "🔧" in result.text
    assert "\n" in result.text


def test_render_long_tool_result_truncated():
    fmt = TextFormatter()
    long_content = "x" * 5000
    event = OutboundEvent(type=OutboundEventType.TOOL_RESULT, content=long_content)
    result = fmt.render(event)
    assert len(result.text) < len(long_content)
    assert "omitted" in result.text
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_formatters_text.py -v`
Expected: FAIL (TextFormatter not found)

**Step 3: Write implementation**

Create `src/pynchy/host/orchestrator/messaging/formatters/text.py`:

```python
"""TextFormatter — default plain-text renderer.

Captures the existing rendering logic from formatter.py as a reusable
BaseFormatter implementation. New channel plugins can use this as-is
for their MVP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.host.orchestrator.messaging.formatter import (
    format_internal_tags,
    format_tool_preview,
)
from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter, RenderedMessage

if TYPE_CHECKING:
    from pynchy.types import OutboundEvent, OutboundEventType

# Channel broadcast truncation threshold for tool results.
_MAX_TOOL_OUTPUT = 4000


def _truncate_output(content: str) -> str:
    """Truncate long tool output, keeping head and tail."""
    head = content[:2000]
    tail = content[-500:]
    omitted = len(content) - 2500
    return f"{head}\n\n... ({omitted} chars omitted) ...\n\n{tail}"


class TextFormatter(BaseFormatter):
    """Default plain-text renderer — the reference implementation for new channels."""

    def render(self, event: OutboundEvent) -> RenderedMessage:
        from pynchy.types import OutboundEventType

        match event.type:
            case OutboundEventType.TEXT:
                text = format_internal_tags(event.content)
                if event.metadata.get("cursor"):
                    text += " \u258c"
                return RenderedMessage(text=text)

            case OutboundEventType.TOOL_TRACE:
                tool_name = event.metadata.get("tool_name", "")
                tool_input = event.metadata.get("tool_input", {})
                preview = format_tool_preview(tool_name, tool_input)
                return RenderedMessage(text=f"\U0001f527 {preview}")

            case OutboundEventType.TOOL_RESULT:
                content = event.content
                tool_name = event.metadata.get("tool_name", "")
                verbose = event.metadata.get("verbose", False)
                if verbose and content:
                    display = (
                        _truncate_output(content)
                        if len(content) > _MAX_TOOL_OUTPUT
                        else content
                    )
                    return RenderedMessage(text=f"\U0001f4cb {tool_name}:\n{display}")
                return RenderedMessage(text="\U0001f4cb tool result")

            case OutboundEventType.THINKING:
                content = event.content
                if content:
                    display = (
                        _truncate_output(content)
                        if len(content) > _MAX_TOOL_OUTPUT
                        else content
                    )
                    return RenderedMessage(text=f"\U0001f4ad {display}")
                return RenderedMessage(text="\U0001f4ad thinking...")

            case OutboundEventType.RESULT:
                text = format_internal_tags(event.content)
                prefix = (
                    "\U0001f99e "
                    if event.metadata.get("prefix_assistant_name", True)
                    else ""
                )
                return RenderedMessage(text=f"{prefix}{text}")

            case OutboundEventType.HOST:
                return RenderedMessage(text=f"\U0001f3e0 {event.content}")

            case OutboundEventType.SYSTEM:
                return RenderedMessage(text=f"\u2699\ufe0f {event.content}")

            case _:
                return RenderedMessage(text=event.content)

    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage:
        texts = [self.render(e).text for e in events]
        return RenderedMessage(text="\n".join(texts))
```

Update `src/pynchy/host/orchestrator/messaging/formatters/__init__.py`:

```python
"""Formatter protocol and implementations."""

from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter, RenderedMessage
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter

__all__ = ["BaseFormatter", "RenderedMessage", "TextFormatter"]
```

**Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_formatters_text.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/formatters/ tests/test_formatters_text.py
git commit -m "feat: add TextFormatter with existing rendering logic"
```

---

### Task 3: Channel protocol migration + WhatsApp

Update the `Channel` protocol to use `send_event`. Update WhatsApp to implement it.

**Files:**
- Modify: `src/pynchy/types.py:305-367` (Channel protocol)
- Modify: `src/pynchy/plugins/channels/whatsapp/channel.py`
- Test: `tests/test_channel_protocol.py`

**Step 1: Write the failing test**

```python
# tests/test_channel_protocol.py
from pynchy.types import OutboundEvent, OutboundEventType


def test_channel_protocol_requires_send_event():
    """Channel protocol must include send_event, not send_message."""
    from pynchy.types import Channel
    import inspect
    members = {name for name, _ in inspect.getmembers(Channel)}
    assert "send_event" in members


def test_channel_protocol_requires_formatter():
    """Channel protocol must include formatter property."""
    from pynchy.types import Channel
    assert hasattr(Channel, "__protocol_attrs__") or "formatter" in dir(Channel)
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_channel_protocol.py -v`
Expected: FAIL (send_event not on Channel)

**Step 3: Update Channel protocol**

In `src/pynchy/types.py`, replace the Channel protocol (lines 305-367):

```python
@runtime_checkable
class Channel(Protocol):
    name: str
    formatter: BaseFormatter

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        """Send a rendered event to the channel.

        This is THE protocol method for all outbound messages.
        Channels call self.formatter.render(event) and send via
        their internal transport.
        """
        ...

    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult: ...

    # Optional streaming (checked with hasattr at call sites):
    #   post_event(jid, event) -> str | None     (returns message_id)
    #   update_event(jid, message_id, event)     (updates in-place)

    # Optional: typing indicator, group creation, prefix_assistant_name
    # — same as before (hasattr/getattr at call sites).
```

Add `BaseFormatter` import at top of types.py:

```python
from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter
```

IMPORTANT: This will create a circular import since `formatters/base.py` TYPE_CHECKS `OutboundEvent` from `types.py`. The `TYPE_CHECKING` guard in `base.py` already handles this. But `types.py` importing from `formatters/base.py` at runtime will work because `base.py` only imports `OutboundEvent` under `TYPE_CHECKING`.

**Step 3b: Update WhatsApp channel**

In `src/pynchy/plugins/channels/whatsapp/channel.py`:
- Add `from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter`
- Add `self.formatter = TextFormatter()` in `__init__`
- Add `send_event` method that renders and calls internal send
- Keep existing `send_message` as private `_send_text` for internal use (queue flush)

```python
async def send_event(self, jid: str, event: OutboundEvent) -> None:
    rendered = self.formatter.render(event)
    await self._send_text(jid, rendered.text)
```

Rename `send_message` → `_send_text` and update internal callers (queue flush at line ~256).

**Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_channel_protocol.py tests/test_builtin_whatsapp*.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/types.py src/pynchy/plugins/channels/whatsapp/ tests/test_channel_protocol.py
git commit -m "feat: update Channel protocol to send_event, migrate WhatsApp"
```

---

### Task 4: Slack channel migration (TextFormatter initially)

Update `SlackChannel` to implement `send_event`, `post_event`, `update_event`. Use `TextFormatter` initially — `SlackBlocksFormatter` comes in Task 8.

**Files:**
- Modify: `src/pynchy/plugins/channels/slack/_channel.py`
- Test: `tests/test_builtin_slack.py` (update existing tests)

**Step 1: Write the failing test**

```python
# Add to tests/test_builtin_slack.py or new file tests/test_slack_send_event.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from pynchy.types import OutboundEvent, OutboundEventType


@pytest.fixture
def slack_channel():
    """Create a SlackChannel with mocked Slack app."""
    from pynchy.plugins.channels.slack._channel import SlackChannel
    ch = SlackChannel(
        connection_name="test",
        bot_token="xoxb-test",
        app_token="xapp-test",
        chat_names=["general"],
        allow_create=False,
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
    )
    ch._app = MagicMock()
    ch._app.client = MagicMock()
    ch._app.client.chat_postMessage = AsyncMock(return_value={"ts": "123.456"})
    ch._app.client.chat_update = AsyncMock()
    ch._connected = True
    ch._allowed_channel_ids = {"C123"}
    return ch


@pytest.mark.asyncio
async def test_send_event_posts_text(slack_channel):
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello world",
        metadata={"prefix_assistant_name": False},
    )
    await slack_channel.send_event("slack:C123", event)
    slack_channel._app.client.chat_postMessage.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_returns_ts(slack_channel):
    event = OutboundEvent(type=OutboundEventType.TEXT, content="streaming", metadata={"cursor": True})
    ts = await slack_channel.post_event("slack:C123", event)
    assert ts == "123.456"


@pytest.mark.asyncio
async def test_update_event_calls_chat_update(slack_channel):
    event = OutboundEvent(type=OutboundEventType.TEXT, content="final text", metadata={"cursor": False})
    await slack_channel.update_event("slack:C123", "123.456", event)
    slack_channel._app.client.chat_update.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_slack_send_event.py -v`
Expected: FAIL (send_event not implemented)

**Step 3: Update SlackChannel**

In `src/pynchy/plugins/channels/slack/_channel.py`:

Add to `__init__`:
```python
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
self.formatter = TextFormatter()
```

Add `send_event`:
```python
async def send_event(self, jid: str, event: OutboundEvent) -> None:
    if not self._app or not self.owns_jid(jid):
        return
    rendered = self.formatter.render(event)
    channel_id = _channel_id_from_jid(jid)
    if rendered.blocks:
        await self._app.client.chat_postMessage(
            channel=channel_id, text=rendered.text, blocks=rendered.blocks
        )
    else:
        chunks = split_text(rendered.text, max_len=3000)
        for chunk in chunks:
            await self._app.client.chat_postMessage(channel=channel_id, text=chunk)
```

Add `post_event`:
```python
async def post_event(self, jid: str, event: OutboundEvent) -> str | None:
    if not self._app or not self.owns_jid(jid):
        return None
    rendered = self.formatter.render(event)
    channel_id = _channel_id_from_jid(jid)
    kwargs: dict[str, Any] = {"channel": channel_id, "text": rendered.text}
    if rendered.blocks:
        kwargs["blocks"] = rendered.blocks
    resp = await self._app.client.chat_postMessage(**kwargs)
    return resp.get("ts")
```

Add `update_event`:
```python
async def update_event(self, jid: str, message_id: str, event: OutboundEvent) -> None:
    if not self._app or not self.owns_jid(jid):
        return
    rendered = self.formatter.render(event)
    channel_id = _channel_id_from_jid(jid)
    kwargs: dict[str, Any] = {"channel": channel_id, "ts": message_id, "text": rendered.text}
    if rendered.blocks:
        kwargs["blocks"] = rendered.blocks
    await self._app.client.chat_update(**kwargs)
```

Keep `send_message`, `post_message`, `update_message` temporarily — they're still called by the pipeline until Tasks 5-6 migrate it. Remove them after Task 6.

**Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_slack_send_event.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/plugins/channels/slack/_channel.py tests/test_slack_send_event.py
git commit -m "feat: add send_event/post_event/update_event to SlackChannel"
```

---

### Task 5: Pipeline migration — sender.py

Change `broadcast()`, `broadcast_formatted()`, and `finalize_stream_or_broadcast()` to work with `OutboundEvent`. The `broadcast_to_channels` method on `OutputDeps` also changes.

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/sender.py`
- Modify: `src/pynchy/host/orchestrator/messaging/streaming.py:29-39` (OutputDeps protocol)
- Modify: `src/pynchy/host/orchestrator/adapters.py:68-89` (MessageBroadcaster)
- Test: `tests/test_outbound.py` (update), `tests/test_broadcast.py` (update)

**Step 1: Write the failing test**

```python
# tests/test_sender_events.py
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from pynchy.types import OutboundEvent, OutboundEventType
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter


def _make_channel(name: str, jid_prefix: str = "slack:"):
    ch = MagicMock()
    ch.name = name
    ch.is_connected.return_value = True
    ch.owns_jid.side_effect = lambda j: j.startswith(jid_prefix)
    ch.formatter = TextFormatter()
    ch.send_event = AsyncMock()
    return ch


def _make_deps(channels):
    deps = MagicMock()
    type(deps).channels = PropertyMock(return_value=channels)
    type(deps).workspaces = PropertyMock(return_value={})
    return deps


@pytest.mark.asyncio
async def test_broadcast_sends_event_to_channels():
    from pynchy.host.orchestrator.messaging.sender import broadcast
    ch = _make_channel("slack")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_called_once_with("slack:C123", event)
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_sender_events.py -v`
Expected: FAIL (broadcast still expects text str)

**Step 3: Update sender.py**

Key changes to `src/pynchy/host/orchestrator/messaging/sender.py`:

- `broadcast()`: change `text: str` → `event: OutboundEvent`, call `ch.send_event(target_jid, event)`, record `event.content` to ledger
- `broadcast_formatted()`: remove entirely (replaced by `broadcast` with event — callers construct the event themselves)
- `finalize_stream_or_broadcast()`: change `text: str` → `event: OutboundEvent`, use `ch.update_event`/`ch.send_event` instead of `update_message`/`send_message`

Update `OutputDeps` in `streaming.py`:
```python
async def broadcast_to_channels(
    self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
) -> None: ...
```

Update `MessageBroadcaster._broadcast_to_channels` in `adapters.py` to accept `OutboundEvent`.

Update `MessageBroadcaster._broadcast_formatted` → remove.

**Step 4: Run tests**

Run: `uv run python -m pytest tests/test_sender_events.py tests/test_outbound.py tests/test_broadcast.py -v`
Expected: PASS (after updating existing tests to use events)

**Step 5: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/sender.py src/pynchy/host/orchestrator/messaging/streaming.py src/pynchy/host/orchestrator/adapters.py tests/
git commit -m "feat: migrate sender.py to OutboundEvent-based broadcasting"
```

---

### Task 6: Pipeline migration — streaming.py + router.py

`StreamState` holds a mutable `OutboundEvent`. `TraceBatcher` buffers events. Router produces events.

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/streaming.py`
- Modify: `src/pynchy/host/orchestrator/messaging/router.py`
- Test: `tests/test_messaging_router.py` (update)

**Step 1: Write the failing test**

```python
# tests/test_streaming_events.py
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from pynchy.types import OutboundEvent, OutboundEventType
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter


@pytest.mark.asyncio
async def test_stream_state_holds_event():
    from pynchy.host.orchestrator.messaging.streaming import StreamState
    event = OutboundEvent(type=OutboundEventType.TEXT, content="hello")
    state = StreamState(event=event)
    state.event.content += " world"
    assert state.event.content == "hello world"


@pytest.mark.asyncio
async def test_stream_text_uses_post_event():
    from pynchy.host.orchestrator.messaging.streaming import stream_text_to_channels, StreamState
    event = OutboundEvent(type=OutboundEventType.TEXT, content="hello")
    state = StreamState(event=event, last_update=0.0)

    ch = MagicMock()
    ch.name = "slack"
    ch.is_connected.return_value = True
    ch.owns_jid.return_value = True
    ch.formatter = TextFormatter()
    ch.post_event = AsyncMock(return_value="123.456")
    ch.update_event = AsyncMock()

    deps = MagicMock()
    type(deps).channels = PropertyMock(return_value=[ch])

    await stream_text_to_channels(deps, "slack:C123", state, final=True)
    ch.post_event.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_streaming_events.py -v`

**Step 3: Update streaming.py**

Change `StreamState`:
```python
@dataclass
class StreamState:
    event: OutboundEvent
    message_ids: dict[str, str] = field(default_factory=dict)
    last_update: float = 0.0
```

Change `stream_text_to_channels` to:
- Read content from `state.event.content`
- Set `state.event.metadata["cursor"] = not final`
- Call `ch.post_event(target_jid, state.event)` / `ch.update_event(target_jid, msg_id, state.event)`
- Check for `post_event`/`update_event` with `hasattr` instead of `post_message`/`update_message`
- Remove the `format_internal_tags` call and cursor logic — formatters handle that now

Change `TraceBatcher`:
- `_buffers: dict[str, list[OutboundEvent]]`
- `enqueue(chat_jid, event: OutboundEvent)`
- `flush()`: calls `broadcast_to_channels(chat_jid, batch_event)` where batch_event is constructed from the buffered events. Or: iterate targets and call `ch.send_event` with a batch. Simplest: create a single event that joins the content.

Actually for TraceBatcher flush, the cleanest approach: flush sends each event individually wrapped in a combined broadcast. Since `broadcast_to_channels` now takes an `OutboundEvent`, flush can create a synthetic event from the batch:

```python
async def flush(self, chat_jid: str) -> None:
    self._cancel_timer(chat_jid)
    events = self._buffers.pop(chat_jid, [])
    if not events:
        return
    # Batch events into a single broadcast
    combined = OutboundEvent(
        type=OutboundEventType.SYSTEM,
        content="\n".join(e.content for e in events),
        metadata={"batch": [e for e in events]},
    )
    await self._deps.broadcast_to_channels(chat_jid, combined)
```

But this loses type information. Better: have channels handle batches via `formatter.render_batch(events)`. Add a `broadcast_batch` function or have the batcher iterate channels directly.

Simplest: change `enqueue_or_broadcast` to take `OutboundEvent`, and flush constructs a batch event.

Change `enqueue_or_broadcast`:
```python
async def enqueue_or_broadcast(deps: OutputDeps, chat_jid: str, event: OutboundEvent) -> None:
```

**Step 3b: Update router.py**

Each handler now produces `OutboundEvent` instead of text:

- `_handle_thinking`: create `OutboundEvent(type=THINKING, content=thinking)`
- `_handle_tool_use`: create `OutboundEvent(type=TOOL_TRACE, metadata={tool_name, tool_input})`
- `_handle_tool_result`: create `OutboundEvent(type=TOOL_RESULT, content=content, metadata={verbose, tool_name})`
- `_handle_system`: create `OutboundEvent(type=SYSTEM, content=channel_text)`
- `_handle_text`: create/grow `StreamState.event`
- `_handle_final_result`: create `OutboundEvent(type=RESULT or HOST, content=text, metadata={prefix_assistant_name})`

`broadcast_trace` changes `channel_text: str` → `event: OutboundEvent`.

`broadcast_agent_input` wraps synthetic messages in `OutboundEvent(type=SYSTEM)`.

**Step 4: Run all tests**

Run: `uv run python -m pytest tests/test_streaming_events.py tests/test_messaging_router.py -v`
Expected: PASS (after updating existing router tests)

**Step 5: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/streaming.py src/pynchy/host/orchestrator/messaging/router.py tests/
git commit -m "feat: migrate streaming and router to OutboundEvent pipeline"
```

---

### Task 7: Migrate remaining callers + cleanup

Update all remaining `broadcast_to_channels(jid, text)` callers to wrap text in `OutboundEvent`. Remove old `send_message`/`post_message`/`update_message` from channels. Remove `broadcast_formatted` and `format_outbound`.

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/approval_handler.py` (4 call sites)
- Modify: `src/pynchy/host/orchestrator/messaging/inbound.py` (2 call sites)
- Modify: `src/pynchy/host/orchestrator/messaging/reaction_handler.py` (1 call site)
- Modify: `src/pynchy/host/container_manager/ipc/handlers_security.py` (1 call site)
- Modify: `src/pynchy/host/container_manager/ipc/handlers_service.py` (1 call site)
- Modify: `src/pynchy/host/orchestrator/adapters.py:140-141` (HostMessageBroadcaster)
- Modify: `src/pynchy/host/orchestrator/messaging/reconciler.py:199` (send_message → send_event)
- Modify: `src/pynchy/plugins/channels/slack/_channel.py` (remove old send_message/post_message/update_message)
- Modify: `src/pynchy/host/orchestrator/messaging/formatter.py` (remove format_outbound — kept utilities)

**Step 1: Write a test that all old call paths are gone**

```python
# tests/test_no_send_message.py
"""Verify that send_message is no longer on the Channel protocol."""
import ast
import inspect

def test_channel_protocol_no_send_message():
    from pynchy.types import Channel
    assert not hasattr(Channel, "send_message") or "send_message" not in {
        name for name, _ in inspect.getmembers(Channel) if not name.startswith("_")
    }
```

**Step 2: Migration pattern**

Each caller wraps its text in an `OutboundEvent`:

```python
# Before:
await deps.broadcast_to_channels(chat_jid, f"🏠 {text}")

# After:
from pynchy.types import OutboundEvent, OutboundEventType
await deps.broadcast_to_channels(
    chat_jid,
    OutboundEvent(type=OutboundEventType.HOST, content=text),
)
```

For `HostMessageBroadcaster._store_broadcast_and_emit` (adapters.py:140):
```python
# Before:
channel_text = f"{channel_emoji} {text}"
await self.broadcaster._broadcast_to_channels(chat_jid, channel_text)

# After:
event = OutboundEvent(type=OutboundEventType.HOST, content=text)
await self.broadcaster._broadcast_to_channels(chat_jid, event)
```

For reconciler retry (reconciler.py:199):
```python
# Before:
await ch.send_message(target_jid, row.content)

# After:
event = OutboundEvent(type=OutboundEventType.RESULT, content=row.content)
await ch.send_event(target_jid, event)
```

**Step 3: Remove old methods from Slack/WhatsApp**

Remove `send_message`, `post_message`, `update_message` from `SlackChannel`.
Remove `send_message` from `WhatsAppChannel` (now `_send_text`).

**Step 4: Remove `format_outbound` and `broadcast_formatted`**

In `formatter.py`: remove `format_outbound()` (lines 79-86). Keep all utility functions.
In `sender.py`: remove `broadcast_formatted()` (lines 207-241).
In `adapters.py`: remove `_broadcast_formatted` (lines 80-89).

**Step 5: Run full test suite**

Run: `uv run python -m pytest -v`
Expected: PASS

**Step 6: Commit**

```bash
git add -A
git commit -m "feat: complete pipeline migration to OutboundEvent, remove legacy send_message"
```

---

### Task 8: SlackBlocksFormatter

Create the rich Slack Block Kit renderer and wire it into `SlackChannel`.

**Files:**
- Create: `src/pynchy/plugins/channels/slack/_blocks.py`
- Modify: `src/pynchy/plugins/channels/slack/_channel.py` (swap formatter)
- Test: `tests/test_slack_blocks_formatter.py`

**Step 1: Write the failing test**

```python
# tests/test_slack_blocks_formatter.py
from pynchy.plugins.channels.slack._blocks import SlackBlocksFormatter
from pynchy.types import OutboundEvent, OutboundEventType


def test_render_result_uses_markdown_block():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="# Hello\n\nThis is **bold** and `code`.",
        metadata={"prefix_assistant_name": False},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    assert any(b["type"] == "markdown" for b in result.blocks)
    assert result.text  # fallback text always present


def test_render_tool_trace_bash_has_context_and_code():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TOOL_TRACE,
        content="",
        metadata={"tool_name": "Bash", "tool_input": {"command": "git status"}},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    block_types = [b["type"] for b in result.blocks]
    assert "context" in block_types
    assert "rich_text" in block_types


def test_render_thinking_uses_context():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(type=OutboundEventType.THINKING, content="analyzing the code structure")
    result = fmt.render(event)
    assert result.blocks is not None
    assert result.blocks[0]["type"] == "context"


def test_render_text_streaming_uses_markdown():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="Working on it...",
        metadata={"cursor": True, "streaming": True},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    assert any(b["type"] == "markdown" for b in result.blocks)
    assert "▌" in result.text


def test_render_tool_result_uses_preformatted():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TOOL_RESULT,
        content="src/main.py\nsrc/utils.py",
    )
    result = fmt.render(event)
    assert result.blocks is not None
    # Should have context header + rich_text preformatted
    assert any(b["type"] == "context" for b in result.blocks)


def test_render_batch_respects_50_block_limit():
    fmt = SlackBlocksFormatter()
    # Create many events that would exceed 50 blocks
    events = [
        OutboundEvent(
            type=OutboundEventType.TOOL_TRACE,
            content="",
            metadata={"tool_name": "Read", "tool_input": {"file_path": f"/path/{i}"}},
        )
        for i in range(30)
    ]
    result = fmt.render_batch(events)
    assert result.blocks is not None
    assert len(result.blocks) <= 50


def test_render_host_uses_context():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(type=OutboundEventType.HOST, content="deployment started")
    result = fmt.render(event)
    assert result.blocks is not None
    assert result.blocks[0]["type"] == "context"
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_slack_blocks_formatter.py -v`

**Step 3: Implement SlackBlocksFormatter**

Create `src/pynchy/plugins/channels/slack/_blocks.py` implementing the design from the design doc:
- `markdown` blocks for TEXT/RESULT
- `context` + `rich_text_preformatted` for TOOL_TRACE
- `context` for THINKING, SYSTEM, HOST
- `render_batch` with 50-block budget
- Full fallback text on every RenderedMessage

See design doc Section 3 for block type mapping.

**Step 3b: Wire into SlackChannel**

In `_channel.py` `__init__`:
```python
# Before:
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
self.formatter = TextFormatter()

# After:
from pynchy.plugins.channels.slack._blocks import SlackBlocksFormatter
self.formatter = SlackBlocksFormatter()
```

**Step 4: Run tests**

Run: `uv run python -m pytest tests/test_slack_blocks_formatter.py tests/test_slack_send_event.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/plugins/channels/slack/_blocks.py src/pynchy/plugins/channels/slack/_channel.py tests/test_slack_blocks_formatter.py
git commit -m "feat: add SlackBlocksFormatter with Block Kit rendering"
```

---

### Task 9: Interactive — approval buttons + stop button

Add `context_actions` blocks for approval and "Stop" button on streaming messages.

**Files:**
- Modify: `src/pynchy/plugins/channels/slack/_blocks.py` (approval block rendering)
- Modify: `src/pynchy/plugins/channels/slack/_channel.py` (interaction handlers)
- Modify: `src/pynchy/host/container_manager/security/approval.py` (add metadata to notification)
- Test: `tests/test_slack_approval_buttons.py`

**Step 1: Write the failing test**

```python
# tests/test_slack_approval_buttons.py
from pynchy.plugins.channels.slack._blocks import SlackBlocksFormatter
from pynchy.types import OutboundEvent, OutboundEventType


def test_approval_event_has_buttons():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.HOST,
        content="Approval required: x_post",
        metadata={
            "approval": True,
            "short_id": "a1",
            "operation": "x_post",
            "details": {"text": "Hello world"},
        },
    )
    result = fmt.render(event)
    assert result.blocks is not None
    # Should have context_actions with approve/deny buttons
    action_blocks = [b for b in result.blocks if b["type"] == "context_actions"]
    assert len(action_blocks) == 1
    elements = action_blocks[0]["elements"]
    action_ids = [e.get("action_id", "") for e in elements]
    assert any("approve" in aid for aid in action_ids)
    assert any("deny" in aid for aid in action_ids)


def test_stop_button_on_streaming():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="Working...",
        metadata={"cursor": True, "streaming": True, "group_name": "ops"},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    assert len(action_blocks) == 1
    assert "stop" in action_blocks[0]["elements"][0].get("action_id", "")
```

**Step 2-4: Implement and test**

Add approval rendering to `SlackBlocksFormatter.render()` — when event has `metadata["approval"]`, append `context_actions` block with approve/deny buttons keyed by `metadata["short_id"]`.

Add stop button to TEXT events when `metadata.get("streaming")` — append `actions` block with Stop button keyed by `metadata.get("group_name")`.

Add interaction handlers to `_channel.py`:
- `cop_approve_{short_id}` / `cop_deny_{short_id}` → route to existing `process_approval_decision()`
- `agent_stop_{group_name}` → send cancellation signal

Update approval notification callers to add `metadata={"approval": True, "short_id": ..., "operation": ..., "details": ...}` to the OutboundEvent.

**Step 5: Commit**

```bash
git add src/pynchy/plugins/channels/slack/ src/pynchy/host/container_manager/security/ tests/test_slack_approval_buttons.py
git commit -m "feat: add approval buttons and stop button for Slack"
```

---

### Task 10: Interactive — improved ask_user

Upgrade the ask_user Block Kit with radio buttons for single-select and checkboxes for multi-select.

**Files:**
- Modify: `src/pynchy/plugins/channels/slack/_ui.py`
- Modify: `src/pynchy/plugins/channels/slack/_channel.py` (handler updates)
- Test: `tests/test_slack_ask_user.py` (update/extend)

**Step 1: Write the failing test**

```python
# tests/test_slack_ask_user_v2.py
from pynchy.plugins.channels.slack._ui import build_ask_user_blocks


def test_single_select_uses_radio_buttons():
    questions = [
        {"question": "Pick one", "options": [
            {"label": "A", "description": "first"},
            {"label": "B", "description": "second"},
        ]}
    ]
    blocks = build_ask_user_blocks("req1", questions)
    # Should contain radio_buttons element
    action_blocks = [b for b in blocks if b["type"] == "actions" or b["type"] == "section"]
    has_radio = any(
        "radio_buttons" in str(b)
        for b in blocks
    )
    assert has_radio


def test_multi_select_uses_checkboxes():
    questions = [
        {"question": "Pick many", "multiSelect": True, "options": [
            {"label": "X", "description": "ex"},
            {"label": "Y", "description": "why"},
        ]}
    ]
    blocks = build_ask_user_blocks("req2", questions)
    has_checkboxes = any("checkboxes" in str(b) for b in blocks)
    assert has_checkboxes


def test_many_options_falls_back_to_buttons():
    questions = [
        {"question": "Pick one", "options": [
            {"label": f"Option {i}", "description": f"desc {i}"}
            for i in range(6)
        ]}
    ]
    blocks = build_ask_user_blocks("req3", questions)
    # > 4 options should use buttons, not radio
    has_radio = any("radio_buttons" in str(b) for b in blocks)
    assert not has_radio
```

**Step 2-4: Implement and test**

Update `build_ask_user_blocks` in `_ui.py`:
- If `question.get("multiSelect")` and <= 4 options: use `checkboxes` element
- If single-select and <= 4 options: use `radio_buttons` element
- If > 4 options: keep current button layout (fallback)

Update `_on_ask_user_interaction` in `_channel.py` to handle radio/checkbox payloads (different `action_id` patterns and value extraction).

**Step 5: Commit**

```bash
git add src/pynchy/plugins/channels/slack/_ui.py src/pynchy/plugins/channels/slack/_channel.py tests/test_slack_ask_user_v2.py
git commit -m "feat: improved ask_user with radio buttons and checkboxes"
```

---

### Task 11: Update existing tests + final verification

Update all existing test files that reference the old `send_message`/`broadcast(text)` API. Run full suite.

**Files:**
- Modify: `tests/test_messaging_formatter.py`
- Modify: `tests/test_outbound.py`
- Modify: `tests/test_broadcast.py`
- Modify: `tests/test_messaging_router.py`
- Modify: `tests/test_builtin_slack.py`
- Modify: `tests/test_messaging_approval.py`
- Modify: `tests/test_messaging_ask_user_e2e.py`

**Step 1: Grep for all `send_message` references in tests**

Run: `rg 'send_message|broadcast_to_channels.*text|\.broadcast\(.*text' tests/`

Update each test to use `send_event`/`OutboundEvent` equivalents.

**Step 2: Run full test suite**

Run: `uv run python -m pytest -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add tests/
git commit -m "test: update all tests for OutboundEvent pipeline"
```
