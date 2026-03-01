"""Tests for Slack interactive buttons — approval and stop button rendering.

Validates that:
- HOST events with approval metadata render Approve/Deny action buttons
- TEXT events with streaming metadata render a Stop button
- Non-approval HOST events and non-streaming TEXT events have no action buttons
"""

from __future__ import annotations

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
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    assert len(action_blocks) >= 1
    elements = action_blocks[0]["elements"]
    action_ids = [e.get("action_id", "") for e in elements]
    assert any("approve" in aid for aid in action_ids)
    assert any("deny" in aid for aid in action_ids)


def test_approval_buttons_encode_short_id():
    """Approve/Deny buttons should encode the approval short_id in their action_id."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.HOST,
        content="Approval required: x_post",
        metadata={
            "approval": True,
            "short_id": "z9",
            "operation": "x_post",
        },
    )
    result = fmt.render(event)
    assert result.blocks is not None
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    elements = action_blocks[0]["elements"]
    action_ids = [e.get("action_id", "") for e in elements]
    assert "cop_approve_z9" in action_ids
    assert "cop_deny_z9" in action_ids


def test_approval_buttons_have_correct_styles():
    """Approve button should be 'primary', Deny button should be 'danger'."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.HOST,
        content="Approval required: x_post",
        metadata={
            "approval": True,
            "short_id": "a1",
            "operation": "x_post",
        },
    )
    result = fmt.render(event)
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    elements = action_blocks[0]["elements"]
    approve_btn = next(e for e in elements if "approve" in e.get("action_id", ""))
    deny_btn = next(e for e in elements if "deny" in e.get("action_id", ""))
    assert approve_btn["style"] == "primary"
    assert deny_btn["style"] == "danger"


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


def test_stop_button_encodes_group_name():
    """Stop button action_id should encode the group name."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="Working...",
        metadata={"cursor": True, "streaming": True, "group_name": "my-project"},
    )
    result = fmt.render(event)
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    assert action_blocks[0]["elements"][0]["action_id"] == "agent_stop_my-project"


def test_no_stop_button_when_not_streaming():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="Final text",
        metadata={"cursor": False},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    assert len(action_blocks) == 0


def test_no_stop_button_when_cursor_true_but_no_group_name():
    """Stop button requires group_name in metadata to know which agent to stop."""
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.TEXT,
        content="Working...",
        metadata={"cursor": True, "streaming": True},
    )
    result = fmt.render(event)
    assert result.blocks is not None
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    assert len(action_blocks) == 0


def test_approval_host_without_metadata_is_normal_host():
    fmt = SlackBlocksFormatter()
    event = OutboundEvent(
        type=OutboundEventType.HOST,
        content="deployment started",
    )
    result = fmt.render(event)
    assert result.blocks is not None
    action_blocks = [b for b in result.blocks if b["type"] == "actions"]
    assert len(action_blocks) == 0
