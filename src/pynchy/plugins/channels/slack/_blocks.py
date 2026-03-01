"""SlackBlocksFormatter — rich Slack Block Kit renderer.

Renders OutboundEvent objects into Slack Block Kit payloads for rich formatting.
Uses Slack's purpose-built block types:

- ``markdown`` block for TEXT/RESULT (Slack's LLM-optimized block that natively
  renders standard markdown — code fences, tables, lists, headers).
- ``context`` block for muted/secondary info (THINKING, SYSTEM, HOST, tool headers).
- ``rich_text`` with ``rich_text_preformatted`` for code (TOOL_TRACE, TOOL_RESULT).

No truncation on the Slack path — full output is sent and Slack handles collapse
via ``expand: false`` on section blocks and ``rich_text_preformatted`` for code.

The ``text`` field on RenderedMessage is always populated as a plain-text fallback
(Slack uses it for notifications, screen readers, and clients that don't render blocks).
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

# Slack limits messages to 50 blocks.
_MAX_BLOCKS_PER_MESSAGE = 50


def _markdown_block(text: str) -> dict:
    """Build a Slack ``markdown`` block — natively renders standard markdown."""
    return {"type": "markdown", "text": text}


def _context_block(text: str) -> dict:
    """Build a Slack ``context`` block with mrkdwn element — small muted line."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def _rich_text_preformatted_block(code: str) -> dict:
    """Build a ``rich_text`` block containing a ``rich_text_preformatted`` section."""
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_preformatted",
                "elements": [{"type": "text", "text": code}],
            }
        ],
    }


class SlackBlocksFormatter(BaseFormatter):
    """Slack Block Kit renderer for OutboundEvent objects.

    Each event type maps to one or more Block Kit blocks.  The ``render()``
    method returns a ``RenderedMessage`` with both ``blocks`` (for rich
    rendering) and ``text`` (plain-text fallback for notifications).

    ``render_batch()`` concatenates blocks from each event, enforcing the
    50-block-per-message Slack limit by dropping oldest events that would
    exceed the budget.
    """

    def render(self, event: OutboundEvent) -> RenderedMessage:
        from pynchy.types import OutboundEventType

        match event.type:
            case OutboundEventType.TEXT:
                return self._render_text(event)
            case OutboundEventType.RESULT:
                return self._render_result(event)
            case OutboundEventType.TOOL_TRACE:
                return self._render_tool_trace(event)
            case OutboundEventType.TOOL_RESULT:
                return self._render_tool_result(event)
            case OutboundEventType.THINKING:
                return self._render_thinking(event)
            case OutboundEventType.HOST:
                return self._render_host(event)
            case OutboundEventType.SYSTEM:
                return self._render_system(event)
            case _:
                return RenderedMessage(text=event.content)

    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage:
        """Render multiple events as a single block list.

        Concatenates blocks from each event, respecting the 50-block budget.
        When the budget is exhausted, remaining events are dropped (their
        fallback text is still included in the text field).
        """
        if not events:
            return RenderedMessage(text="", blocks=[])

        all_blocks: list[dict] = []
        all_texts: list[str] = []

        for event in events:
            rendered = self.render(event)
            if rendered.text:
                all_texts.append(rendered.text)
            if (
                rendered.blocks
                and len(all_blocks) + len(rendered.blocks) <= _MAX_BLOCKS_PER_MESSAGE
            ):
                all_blocks.extend(rendered.blocks)

        return RenderedMessage(
            text="\n".join(all_texts),
            blocks=all_blocks if all_blocks else None,
        )

    # ------------------------------------------------------------------
    # Per-type renderers
    # ------------------------------------------------------------------

    def _render_text(self, event: OutboundEvent) -> RenderedMessage:
        """TEXT — streaming text updates, rendered as a ``markdown`` block.

        When ``cursor`` is True (actively streaming) and ``group_name`` is present,
        appends a Stop button so the user can cancel the running agent.
        The button is removed on the next update when streaming ends (cursor=False).
        """
        content = format_internal_tags(event.content)
        fallback = content
        cursor = event.metadata.get("cursor", False)
        if cursor:
            content += " \u258c"
            fallback += " \u258c"
        blocks: list[dict] = [_markdown_block(content)]

        if cursor and event.metadata.get("group_name"):
            group = event.metadata["group_name"]
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "\u23f9 Stop"},
                            "action_id": f"agent_stop_{group}",
                            "value": group,
                        }
                    ],
                }
            )

        return RenderedMessage(text=fallback, blocks=blocks)

    def _render_result(self, event: OutboundEvent) -> RenderedMessage:
        """RESULT — final assistant response, rendered as a ``markdown`` block."""
        content = format_internal_tags(event.content)
        fallback = content
        blocks = [_markdown_block(content)]
        return RenderedMessage(text=fallback, blocks=blocks)

    def _render_tool_trace(self, event: OutboundEvent) -> RenderedMessage:
        """TOOL_TRACE — tool invocation header + code preview.

        Produces a ``context`` block with the tool name emoji and a
        ``rich_text`` block with the command/input in a preformatted section.
        """
        tool_name = event.metadata.get("tool_name", "")
        tool_input = event.metadata.get("tool_input", {})
        preview = format_tool_preview(tool_name, tool_input)
        fallback = f"\U0001f527 {preview}"

        # Context header: tool name with emoji
        header = _context_block(f"\U0001f527 *{tool_name}*")

        # Code block: extract the meaningful input for preformatted display
        code_content = self._extract_tool_code(tool_name, tool_input)
        code_block = _rich_text_preformatted_block(code_content)

        return RenderedMessage(text=fallback, blocks=[header, code_block])

    def _render_tool_result(self, event: OutboundEvent) -> RenderedMessage:
        """TOOL_RESULT — tool output, rendered as context header + preformatted code."""
        content = event.content
        tool_name = event.metadata.get("tool_name", "")
        verbose = event.metadata.get("verbose", False)

        # Header
        label = tool_name or "tool"
        header = _context_block(f"\U0001f4cb *{label}* result")

        if verbose and content:
            # Verbose tools show full content
            blocks: list[dict] = [header, _rich_text_preformatted_block(content)]
            fallback = f"\U0001f4cb {label}:\n{content}"
        elif content:
            # Non-verbose with content: full output in blocks, compact notification text
            blocks = [header, _rich_text_preformatted_block(content)]
            fallback = f"\U0001f4cb {label} result"
        else:
            blocks = [header]
            fallback = f"\U0001f4cb {label} result"

        return RenderedMessage(text=fallback, blocks=blocks)

    def _render_thinking(self, event: OutboundEvent) -> RenderedMessage:  # noqa: ARG002
        """THINKING — compact muted line in a ``context`` block.

        Always shows a brief "thinking..." indicator regardless of content.
        The actual thought content is internal/secondary and not displayed
        in the Slack block -- the user just sees that the agent is thinking.
        """
        mrkdwn_text = "\U0001f4ad _thinking..._"
        blocks = [_context_block(mrkdwn_text)]
        fallback = "\U0001f4ad thinking..."
        return RenderedMessage(text=fallback, blocks=blocks)

    def _render_host(self, event: OutboundEvent) -> RenderedMessage:
        """HOST — small muted operational line.

        When ``approval`` metadata is True, appends Approve/Deny action buttons
        with action_ids encoding the short_id (e.g. ``cop_approve_a1``).
        The Slack interaction handler routes button clicks to the existing
        approval decision pipeline.
        """
        blocks: list[dict] = [_context_block(f"\U0001f3e0 {event.content}")]

        if event.metadata.get("approval"):
            short_id = event.metadata.get("short_id", "")
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "\u2705 Approve"},
                            "action_id": f"cop_approve_{short_id}",
                            "style": "primary",
                            "value": short_id,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "\u274c Deny"},
                            "action_id": f"cop_deny_{short_id}",
                            "style": "danger",
                            "value": short_id,
                        },
                    ],
                }
            )

        return RenderedMessage(text=f"\U0001f3e0 {event.content}", blocks=blocks)

    def _render_system(self, event: OutboundEvent) -> RenderedMessage:
        """SYSTEM — small muted operational line."""
        blocks = [_context_block(f"\u2699\ufe0f {event.content}")]
        return RenderedMessage(text=f"\u2699\ufe0f {event.content}", blocks=blocks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_code(tool_name: str, tool_input: dict) -> str:
        """Extract the most relevant code/input string for preformatted display."""
        if tool_name == "Bash":
            return tool_input.get("command", tool_name)
        if tool_name == "Read":
            return tool_input.get("file_path", tool_name)
        if tool_name == "Edit":
            path = tool_input.get("file_path", "")
            old = tool_input.get("old_string", "")
            new = tool_input.get("new_string", "")
            parts = []
            if path:
                parts.append(path)
            if old:
                parts.append(f"- {old}")
            if new:
                parts.append(f"+ {new}")
            return "\n".join(parts) if parts else tool_name
        if tool_name == "Write":
            path = tool_input.get("file_path", "")
            return path or tool_name
        if tool_name == "Grep":
            pattern = tool_input.get("pattern", "")
            path = tool_input.get("path", "")
            return f"/{pattern}/ {path}".strip() if pattern else tool_name
        if tool_name == "Glob":
            return tool_input.get("pattern", tool_name)
        # Fallback: stringify the input
        preview = str(tool_input)
        return preview if tool_input else tool_name
