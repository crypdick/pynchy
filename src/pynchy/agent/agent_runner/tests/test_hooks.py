"""Tests for hook event system extensions."""

from agent_runner.hooks import (
    AGNOSTIC_TO_CLAUDE,
    CLAUDE_HOOK_MAP,
    HookDecision,
    HookEvent,
    before_tool_use_roster,
    builtin_before_tool_hooks,
)


def test_before_tool_use_event_exists():
    assert hasattr(HookEvent, "BEFORE_TOOL_USE")
    assert HookEvent.BEFORE_TOOL_USE.value == "before_tool_use"


def test_before_tool_use_maps_to_claude_pre_tool_use():
    assert CLAUDE_HOOK_MAP["PreToolUse"] == HookEvent.BEFORE_TOOL_USE
    assert AGNOSTIC_TO_CLAUDE[HookEvent.BEFORE_TOOL_USE] == "PreToolUse"


def test_hook_decision_defaults():
    decision = HookDecision()
    assert decision.allowed is True
    assert decision.reason is None


def test_hook_decision_deny():
    decision = HookDecision(allowed=False, reason="blocked by policy")
    assert decision.allowed is False
    assert decision.reason == "blocked by policy"


# ---------------------------------------------------------------------------
# before_tool_use_roster: the single-source security gate composition
# ---------------------------------------------------------------------------


def test_roster_is_builtins_only_when_no_plugin_hooks():
    empty = {event: [] for event in HookEvent}
    roster = before_tool_use_roster(empty)
    assert roster == builtin_before_tool_hooks()


def test_roster_puts_builtins_before_plugin_hooks():
    async def plugin_hook(tool_name, tool_input):  # pragma: no cover - identity only
        ...

    loaded = {event: [] for event in HookEvent}
    loaded[HookEvent.BEFORE_TOOL_USE] = [plugin_hook]

    roster = before_tool_use_roster(loaded)
    builtins = builtin_before_tool_hooks()

    # Built-ins first (in their fixed order), plugin hooks appended last.
    assert roster[: len(builtins)] == builtins
    assert roster[-1] is plugin_hook
    assert len(roster) == len(builtins) + 1


def test_roster_ignores_non_before_tool_use_events():
    loaded = {event: [] for event in HookEvent}
    # A plugin registering only a compact hook must not add to the tool gate.
    loaded[HookEvent.BEFORE_COMPACT] = [lambda *a, **k: None]

    assert before_tool_use_roster(loaded) == builtin_before_tool_hooks()
