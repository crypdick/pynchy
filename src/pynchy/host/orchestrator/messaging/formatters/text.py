"""TextFormatter -- default plain-text renderer.

Captures the existing rendering logic from formatter.py and router.py as a
reusable BaseFormatter implementation.  New channel plugins can use this as-is
for their MVP; richer channels (e.g. Slack) subclass or replace it with
block-based formatters.

Imports utility functions from the existing ``formatter`` module rather than
duplicating them:
  - ``format_internal_tags``  -- converts ``<internal>`` tags to italicised thoughts
  - ``format_tool_preview``   -- one-line preview of a tool invocation

The ``_truncate_output`` helper was originally in ``router.py`` (line ~80);
it is re-implemented here as a module-level function so TextFormatter is
self-contained and the old router code can eventually delegate to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.host.orchestrator.messaging.formatter import (
    format_internal_tags,
    format_tool_preview,
)
from pynchy.host.orchestrator.messaging.formatters.base import BaseFormatter, RenderedMessage

if TYPE_CHECKING:
    from pynchy.types import OutboundEvent

# Channel broadcast truncation threshold for tool results.
# Mirrors ``_MAX_TOOL_OUTPUT`` in router.py — full content is always persisted
# to the DB; only the channel broadcast is truncated.
_MAX_TOOL_OUTPUT = 4000


def _truncate_output(content: str) -> str:
    """Truncate long tool output for channel broadcast, keeping head and tail.

    Keeps the first 2000 characters and the last 500 characters, inserting
    an ``... (N chars omitted) ...`` marker in between.
    """
    head = content[:2000]
    tail = content[-500:]
    omitted = len(content) - 2500
    return f"{head}\n\n... ({omitted} chars omitted) ...\n\n{tail}"


class TextFormatter(BaseFormatter):
    """Default plain-text renderer -- the reference implementation for new channels.

    Each ``OutboundEventType`` maps to a rendering path that mirrors what
    ``router.py`` currently produces inline.  Extracting it here lets us
    share the logic across channels and test it without async infrastructure.
    """

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
                # Only verbose tools (e.g. ExitPlanMode) show their result
                # content in the channel broadcast.  All others get a compact
                # placeholder -- the full content is still persisted to the DB
                # by the router layer.
                if verbose and content:
                    display = (
                        _truncate_output(content) if len(content) > _MAX_TOOL_OUTPUT else content
                    )
                    return RenderedMessage(text=f"\U0001f4cb {tool_name}:\n{display}")
                return RenderedMessage(text="\U0001f4cb tool result")

            case OutboundEventType.THINKING:
                content = event.content
                if content:
                    display = (
                        _truncate_output(content) if len(content) > _MAX_TOOL_OUTPUT else content
                    )
                    return RenderedMessage(text=f"\U0001f4ad {display}")
                return RenderedMessage(text="\U0001f4ad thinking...")

            case OutboundEventType.RESULT:
                text = format_internal_tags(event.content)
                prefix = "\U0001f99e " if event.metadata.get("prefix_assistant_name", True) else ""
                return RenderedMessage(text=f"{prefix}{text}")

            case OutboundEventType.HOST:
                return RenderedMessage(text=f"\U0001f3e0 {event.content}")

            case OutboundEventType.SYSTEM:
                return RenderedMessage(text=f"\u2699\ufe0f {event.content}")

            case _:
                return RenderedMessage(text=event.content)

    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage:
        """Render multiple events as a single newline-joined message."""
        texts = [self.render(e).text for e in events]
        return RenderedMessage(text="\n".join(texts))
