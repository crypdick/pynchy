"""Tests for hook event system extensions."""

from agent_runner.hooks import AGNOSTIC_TO_CLAUDE, CLAUDE_HOOK_MAP, HookEvent


def test_before_tool_use_event_exists():
    assert hasattr(HookEvent, "BEFORE_TOOL_USE")
    assert HookEvent.BEFORE_TOOL_USE.value == "before_tool_use"


def test_before_tool_use_maps_to_claude_pre_tool_use():
    assert CLAUDE_HOOK_MAP["PreToolUse"] == HookEvent.BEFORE_TOOL_USE
    assert AGNOSTIC_TO_CLAUDE[HookEvent.BEFORE_TOOL_USE] == "PreToolUse"


def test_hook_decision_defaults():
    from agent_runner.hooks import HookDecision

    decision = HookDecision()
    assert decision.allowed is True
    assert decision.reason is None


def test_hook_decision_deny():
    from agent_runner.hooks import HookDecision

    decision = HookDecision(allowed=False, reason="blocked by policy")
    assert decision.allowed is False
    assert decision.reason == "blocked by policy"
