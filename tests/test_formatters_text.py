"""Tests for TextFormatter — the default plain-text renderer."""

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
    assert "\U0001f527" in result.text
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
    assert result.text.startswith("\U0001f99e ")
    assert "Here is the answer" in result.text


def test_render_result_no_prefix():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Here is the answer",
        metadata={"prefix_assistant_name": False},
    )
    result = fmt.render(event)
    assert not result.text.startswith("\U0001f99e")


def test_render_text_with_cursor():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="streaming text",
        metadata={"cursor": True},
    )
    result = fmt.render(event)
    assert result.text.endswith(" \u258c")


def test_render_text_no_cursor():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="final text",
        metadata={"cursor": False},
    )
    result = fmt.render(event)
    assert "\u258c" not in result.text


def test_render_thinking():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.THINKING, content="analyzing code")
    result = fmt.render(event)
    assert "\U0001f4ad" in result.text
    assert "analyzing code" in result.text


def test_render_tool_result():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.TOOL_RESULT, content="file contents here")
    result = fmt.render(event)
    assert "\U0001f4cb" in result.text


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
    assert "\u2699\ufe0f" in result.text


def test_render_host():
    fmt = TextFormatter()
    event = OutboundEvent(type=OutboundEventType.HOST, content="deployment started")
    result = fmt.render(event)
    assert "\U0001f3e0" in result.text


def test_render_internal_tags():
    fmt = TextFormatter()
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="Hello <internal>thinking about it</internal> world",
        metadata={"prefix_assistant_name": True},
    )
    result = fmt.render(event)
    assert "<internal>" not in result.text
    assert "\U0001f9e0" in result.text
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
    assert "\U0001f4ad" in result.text
    assert "\U0001f527" in result.text
    assert "\n" in result.text


def test_render_long_tool_result_truncated():
    """Verbose tool results exceeding the threshold are truncated with head+tail."""
    fmt = TextFormatter()
    long_content = "x" * 5000
    event = OutboundEvent(
        type=OutboundEventType.TOOL_RESULT,
        content=long_content,
        metadata={"verbose": True, "tool_name": "ExitPlanMode"},
    )
    result = fmt.render(event)
    assert len(result.text) < len(long_content)
    assert "omitted" in result.text
