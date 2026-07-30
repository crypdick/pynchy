"""Behavioral edge coverage for Slack Block Kit rendering."""

from __future__ import annotations

import pytest

from pynchy.plugins.api import OutboundEvent, OutboundEventType
from pynchy.plugins.channels.slack import SlackBlocksFormatter


def _code_block(result: object) -> dict[str, object]:
    blocks = result.blocks  # type: ignore[attr-defined]
    assert blocks is not None
    return blocks[1]["elements"][0]["elements"][0]  # type: ignore[index]


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected"),
    [
        (
            "Edit",
            {"file_path": "src/example.py", "old_string": "old", "new_string": "new"},
            "src/example.py\n- old\n+ new",
        ),
        ("Grep", {"pattern": "TODO", "path": "src"}, "/TODO/ src"),
        ("Grep", {}, "Grep"),
        ("Custom", {"value": 1}, "{'value': 1}"),
        ("Custom", {}, "Custom"),
    ],
)
def test_tool_trace_renders_each_input_projection(tool_name, tool_input, expected):
    result = SlackBlocksFormatter().render(
        OutboundEvent(
            type=OutboundEventType.TOOL_TRACE,
            content="",
            metadata={"tool_name": tool_name, "tool_input": tool_input},
        )
    )

    assert _code_block(result)["text"] == expected


def test_tool_result_without_content_uses_tool_fallback_label():
    result = SlackBlocksFormatter().render(
        OutboundEvent(
            type=OutboundEventType.TOOL_RESULT,
            content="",
            metadata={"tool_name": "Read"},
        )
    )

    assert result.text == "📋 Read result"
    assert result.blocks is not None
    assert len(result.blocks) == 1


def test_host_approval_renders_approve_and_deny_buttons():
    result = SlackBlocksFormatter().render(
        OutboundEvent(
            type=OutboundEventType.HOST,
            content="Approval required",
            metadata={"approval": True, "short_id": "a1"},
        )
    )

    assert result.blocks is not None
    actions = result.blocks[1]["elements"]
    assert [button["action_id"] for button in actions] == [
        "cop_approve_a1",
        "cop_deny_a1",
    ]


def test_approval_without_short_id_has_no_action_block():
    result = SlackBlocksFormatter().render(
        OutboundEvent(type=OutboundEventType.APPROVAL, content="Approval required")
    )

    assert result.blocks == [{"type": "markdown", "text": "Approval required"}]
