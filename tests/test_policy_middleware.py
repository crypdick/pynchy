"""Tests for policy middleware."""

from __future__ import annotations

import time

from pynchy.policy.middleware import ActionTracker, PolicyDecision, PolicyMiddleware
from pynchy.types import McpToolConfig, RateLimitConfig, WorkspaceSecurity

# --- PolicyDecision ---


def test_policy_decision_defaults():
    d = PolicyDecision(allowed=True)
    assert d.allowed is True
    assert d.reason is None
    assert d.requires_approval is False


# --- Tier-based evaluation ---


def test_always_approve_auto_approved():
    """always-approve tools are auto-approved."""
    security = WorkspaceSecurity(
        mcp_tools={"read_email": McpToolConfig(risk_tier="always-approve", enabled=True)},
        default_risk_tier="human-approval",
    )
    policy = PolicyMiddleware(security)
    decision = policy.evaluate("read_email", {})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_rules_engine_auto_approved():
    """rules-engine tier tools are auto-approved (rules engine is stub for now)."""
    security = WorkspaceSecurity(
        mcp_tools={"create_event": McpToolConfig(risk_tier="rules-engine", enabled=True)},
        default_risk_tier="human-approval",
    )
    policy = PolicyMiddleware(security)
    decision = policy.evaluate("create_event", {})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_human_approval_requires_approval():
    """human-approval tier tools are denied with requires_approval=True."""
    security = WorkspaceSecurity(
        mcp_tools={"send_email": McpToolConfig(risk_tier="human-approval", enabled=True)},
        default_risk_tier="rules-engine",
    )
    policy = PolicyMiddleware(security)
    decision = policy.evaluate("send_email", {"to": "test@example.com"})

    assert decision.allowed is False
    assert decision.requires_approval is True


def test_disabled_tool_denied():
    """Disabled tools are rejected regardless of tier."""
    security = WorkspaceSecurity(
        mcp_tools={"send_email": McpToolConfig(risk_tier="human-approval", enabled=False)},
        default_risk_tier="rules-engine",
    )
    policy = PolicyMiddleware(security)
    decision = policy.evaluate("send_email", {})

    assert decision.allowed is False
    assert "disabled" in decision.reason.lower()


def test_unconfigured_tool_uses_default_tier():
    """Tools not in mcp_tools use the default_risk_tier."""
    security = WorkspaceSecurity(
        mcp_tools={},
        default_risk_tier="always-approve",
    )
    policy = PolicyMiddleware(security)
    decision = policy.evaluate("some_new_tool", {})

    assert decision.allowed is True  # always-approve default


def test_unconfigured_tool_default_human_approval():
    """Unconfigured tools with human-approval default require approval."""
    security = WorkspaceSecurity(
        mcp_tools={},
        default_risk_tier="human-approval",
    )
    policy = PolicyMiddleware(security)
    decision = policy.evaluate("new_tool", {})

    assert decision.allowed is False
    assert decision.requires_approval is True


# --- Rate limiting ---


def test_rate_limit_global():
    """Global rate limit blocks after threshold."""
    security = WorkspaceSecurity(
        mcp_tools={"read_email": McpToolConfig(risk_tier="always-approve", enabled=True)},
        default_risk_tier="always-approve",
        rate_limits=RateLimitConfig(max_calls_per_hour=3, per_tool_overrides={}),
    )
    policy = PolicyMiddleware(security)

    # First 3 calls succeed
    for _ in range(3):
        decision = policy.evaluate("read_email", {})
        assert decision.allowed is True

    # 4th call is rate-limited
    decision = policy.evaluate("read_email", {})
    assert decision.allowed is False
    assert "rate limit" in decision.reason.lower()


def test_rate_limit_per_tool():
    """Per-tool rate limit blocks specific tool while others continue."""
    security = WorkspaceSecurity(
        mcp_tools={
            "read_email": McpToolConfig(risk_tier="always-approve", enabled=True),
            "list_calendar": McpToolConfig(risk_tier="always-approve", enabled=True),
        },
        default_risk_tier="always-approve",
        rate_limits=RateLimitConfig(
            max_calls_per_hour=100,
            per_tool_overrides={"read_email": 2},
        ),
    )
    policy = PolicyMiddleware(security)

    # 2 read_email calls succeed
    for _ in range(2):
        decision = policy.evaluate("read_email", {})
        assert decision.allowed is True

    # 3rd read_email is blocked
    decision = policy.evaluate("read_email", {})
    assert decision.allowed is False
    assert "read_email" in decision.reason

    # But list_calendar still works
    decision = policy.evaluate("list_calendar", {})
    assert decision.allowed is True


def test_rate_limit_checked_before_tier():
    """Rate limit blocks even auto-approved always-approve tools."""
    security = WorkspaceSecurity(
        mcp_tools={"read_email": McpToolConfig(risk_tier="always-approve", enabled=True)},
        default_risk_tier="always-approve",
        rate_limits=RateLimitConfig(max_calls_per_hour=1, per_tool_overrides={}),
    )
    policy = PolicyMiddleware(security)

    decision = policy.evaluate("read_email", {})
    assert decision.allowed is True

    # Even though always-approve, rate limit blocks it
    decision = policy.evaluate("read_email", {})
    assert decision.allowed is False


def test_no_rate_limits():
    """No rate limiting when rate_limits is None."""
    security = WorkspaceSecurity(
        mcp_tools={"read_email": McpToolConfig(risk_tier="always-approve", enabled=True)},
        default_risk_tier="always-approve",
        rate_limits=None,
    )
    policy = PolicyMiddleware(security)

    # Many calls should all succeed
    for _ in range(100):
        decision = policy.evaluate("read_email", {})
        assert decision.allowed is True


# --- ActionTracker unit tests ---


def test_action_tracker_prune():
    """Test that old timestamps are pruned from the window."""
    tracker = ActionTracker(RateLimitConfig(max_calls_per_hour=10))
    now = time.monotonic()
    # Old timestamps (2 hours ago) should be pruned
    old = [now - 7200, now - 7201]
    recent = [now - 100, now - 50]
    result = tracker._prune(old + recent, now)
    assert len(result) == 2
    assert result == recent


# --- Mixed scenarios ---


def test_god_workspace_permissive():
    """Test permissive security for god workspaces."""
    security = WorkspaceSecurity(
        mcp_tools={
            "send_message": McpToolConfig(risk_tier="always-approve", enabled=True),
            "schedule_task": McpToolConfig(risk_tier="rules-engine", enabled=True),
        },
        default_risk_tier="rules-engine",
        rate_limits=RateLimitConfig(max_calls_per_hour=500),
    )
    policy = PolicyMiddleware(security)

    assert policy.evaluate("send_message", {}).allowed is True
    assert policy.evaluate("schedule_task", {}).allowed is True
    assert policy.evaluate("some_new_tool", {}).allowed is True  # rules-engine default


def test_strict_workspace():
    """Test strict security for untrusted workspaces."""
    security = WorkspaceSecurity(
        mcp_tools={},
        default_risk_tier="human-approval",
        rate_limits=RateLimitConfig(max_calls_per_hour=30),
    )
    policy = PolicyMiddleware(security)

    # Unconfigured tool uses default tier (human-approval)
    decision = policy.evaluate("anything", {})
    assert decision.allowed is False
    assert decision.requires_approval is True
