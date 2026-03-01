"""Tests for SlackBlocksFormatter — Slack Block Kit rich renderer.

Validates that each OutboundEventType maps to the correct Block Kit structure
and that batch rendering respects Slack's 50-block-per-message limit.
"""

from __future__ import annotations

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
    assert "\u258c" in result.text


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


def test_render_system_uses_context():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(type=OutboundEventType.SYSTEM, content="agent restarted")
    result = fmt.render(event)
    assert result.blocks is not None
    assert result.blocks[0]["type"] == "context"


def test_render_result_with_internal_tags():
    """Internal tags should be processed for the text fallback."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="<internal>thinking about it</internal>\nHere is the answer.",
        metadata={"prefix_assistant_name": False},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    # The markdown block should contain the full content (including internal tags
    # processed or raw -- the blocks are for Slack rendering)
    assert result.text  # fallback text always present


def test_render_tool_trace_read_has_context_and_code():
    """Read tool should also produce context + rich_text blocks."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TOOL_TRACE,
        content="",
        metadata={"tool_name": "Read", "tool_input": {"file_path": "/src/main.py"}},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    block_types = [b["type"] for b in result.blocks]
    assert "context" in block_types
    assert "rich_text" in block_types


def test_render_thinking_empty_content():
    """Empty thinking content should still produce a context block."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(type=OutboundEventType.THINKING, content="")
    result = fmt.render(event)
    assert result.blocks is not None
    assert result.blocks[0]["type"] == "context"


def test_render_tool_result_verbose_shows_content():
    """Verbose tool results should show the content in a preformatted block."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TOOL_RESULT,
        content="Plan complete:\n- Step 1\n- Step 2",
        metadata={"tool_name": "ExitPlanMode", "verbose": True},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    # Should have rich_text with preformatted content
    has_rich_text = any(b["type"] == "rich_text" for b in result.blocks)
    assert has_rich_text


def test_render_batch_combines_blocks():
    """Batch rendering should combine blocks from multiple events."""
    fmt = SlackBlocksFormatter()
    events = [
        OutboundEvent(type=OutboundEventType.HOST, content="starting"),
        OutboundEvent(
            type=OutboundEventType.RESULT,
            content="Done!",
            metadata={"prefix_assistant_name": False},
        ),
    ]
    result = fmt.render_batch(events)
    assert result.blocks is not None
    block_types = [b["type"] for b in result.blocks]
    assert "context" in block_types
    assert "markdown" in block_types


def test_render_batch_empty_events():
    """Batch rendering with no events should return empty text."""
    fmt = SlackBlocksFormatter()
    result = fmt.render_batch([])
    assert result.text == ""


def test_render_text_without_cursor():
    """TEXT event without cursor should not have the block cursor character."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="Just some text",
        metadata={},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    assert any(b["type"] == "markdown" for b in result.blocks)
    assert "\u258c" not in result.text


def test_fallback_text_always_present():
    """Every render call must produce non-empty text for Slack notifications."""
    fmt = SlackBlocksFormatter()
    event_types_and_args = [
        (OutboundEventType.TEXT, "hello", {}),
        (OutboundEventType.RESULT, "done", {"prefix_assistant_name": False}),
        (OutboundEventType.TOOL_TRACE, "", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        (OutboundEventType.TOOL_RESULT, "output", {}),
        (OutboundEventType.THINKING, "hmm", {}),
        (OutboundEventType.HOST, "status", {}),
        (OutboundEventType.SYSTEM, "info", {}),
    ]
    for event_type, content, metadata in event_types_and_args:
        event = OutboundEvent(type=event_type, content=content, metadata=metadata)
        result = fmt.render(event)
        assert result.text, f"Fallback text missing for {event_type}"
