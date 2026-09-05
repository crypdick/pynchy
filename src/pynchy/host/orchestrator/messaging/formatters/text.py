"""TextFormatter -- default plain-text renderer.

A reusable ``Formatter`` implementation. Channel plugins can use this as-is;
richer channels (e.g. Slack) provide their own block-based formatters that
satisfy the same Protocol.

Imports utility functions from the ``formatter`` module rather than
duplicating them:
  - ``format_internal_tags``  -- converts ``<internal>`` tags to italicised thoughts
  - ``format_tool_preview``   -- one-line preview of a tool invocation

The ``_truncate_output`` helper is a module-level function so TextFormatter is
self-contained.
"""

from __future__ import annotations

from pynchy.host.orchestrator.messaging.formatter import (
    format_internal_tags,
    format_tool_preview,
)
from pynchy.host.orchestrator.messaging.formatters.base import RenderedMessage
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)

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


class TextFormatter:
    """Default plain-text renderer -- the reference implementation for channels.

    Each ``OutboundEventType`` maps to a rendering path. Keeping it here lets
    channels share the logic and test it without async infrastructure.
    """

    def render(self, event: OutboundEvent) -> RenderedMessage:
        renderers = {
            OutboundEventType.TEXT: self._render_text,
            OutboundEventType.TOOL_TRACE: self._render_tool_trace,
            OutboundEventType.TOOL_RESULT: self._render_tool_result,
            OutboundEventType.THINKING: self._render_thinking,
            OutboundEventType.RESULT: self._render_result,
            OutboundEventType.HOST: self._render_host,
            OutboundEventType.SYSTEM: self._render_system,
            OutboundEventType.APPROVAL: self._render_approval,
        }
        return renderers[event.type](event)

    def _render_text(self, event: OutboundEvent) -> RenderedMessage:
        text = format_internal_tags(event.content)
        if event.metadata.get("cursor"):
            text += " \u258c"
        return RenderedMessage(text=text)

    def _render_tool_trace(self, event: OutboundEvent) -> RenderedMessage:
        tool_name = event.metadata.get("tool_name", "")
        tool_input = event.metadata.get("tool_input", {})
        preview = format_tool_preview(tool_name, tool_input)
        return RenderedMessage(text=f"\U0001f527 {preview}")

    def _render_tool_result(self, event: OutboundEvent) -> RenderedMessage:
        content = event.content
        if event.metadata.get("verbose") and content:
            tool_name = event.metadata.get("tool_name", "")
            return RenderedMessage(text=f"\U0001f4cb {tool_name}:\n{_display_content(content)}")
        return RenderedMessage(text="\U0001f4cb tool result")

    def _render_thinking(self, event: OutboundEvent) -> RenderedMessage:
        if event.content:
            return RenderedMessage(text=f"\U0001f4ad {_display_content(event.content)}")
        return RenderedMessage(text="\U0001f4ad thinking...")

    def _render_result(self, event: OutboundEvent) -> RenderedMessage:
        text = format_internal_tags(event.content)
        prefix = "\U0001f99e " if event.metadata.get("prefix_assistant_name", True) else ""
        return RenderedMessage(text=f"{prefix}{text}")

    def _render_host(self, event: OutboundEvent) -> RenderedMessage:
        return RenderedMessage(text=f"\U0001f3e0 {event.content}")

    def _render_system(self, event: OutboundEvent) -> RenderedMessage:
        return RenderedMessage(text=f"\u2699\ufe0f {event.content}")

    def _render_approval(self, event: OutboundEvent) -> RenderedMessage:
        return RenderedMessage(text=event.content)


def _display_content(content: str) -> str:
    return _truncate_output(content) if len(content) > _MAX_TOOL_OUTPUT else content
