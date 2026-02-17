"""Policy enforcement middleware for IPC requests.

Evaluates tool calls against workspace security profiles using
deterministic, host-side rules. The agent cannot bypass these gates —
they run in the host process, not inside the container.

Risk tiers:
  - "always-approve"   → auto-approved, no check needed
  - "rules-engine"     → deterministic rules engine (auto-approve for now)
  - "human-approval"   → denied until human approves via chat
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from pynchy.types import RateLimitConfig, WorkspaceSecurity


class PolicyDeniedError(Exception):
    """Raised when policy denies a request. Non-retryable."""


@dataclass
class PolicyDecision:
    """Result of policy evaluation."""

    allowed: bool
    reason: str | None = None
    requires_approval: bool = False


class ActionTracker:
    """Sliding-window rate limiter for tool calls."""

    def __init__(self, rate_limits: RateLimitConfig) -> None:
        self._rate_limits = rate_limits
        self._timestamps: list[float] = []
        self._per_tool: dict[str, list[float]] = defaultdict(list)
        self._window_seconds = 3600  # 1 hour

    def _prune(self, timestamps: list[float], now: float) -> list[float]:
        cutoff = now - self._window_seconds
        return [t for t in timestamps if t > cutoff]

    def check_and_record(self, tool_name: str) -> tuple[bool, str | None]:
        """Check rate limit and record the call if allowed.

        Returns (allowed, reason). reason is None when allowed.
        """
        now = time.monotonic()

        self._timestamps = self._prune(self._timestamps, now)
        self._per_tool[tool_name] = self._prune(self._per_tool[tool_name], now)

        # Global limit
        if len(self._timestamps) >= self._rate_limits.max_calls_per_hour:
            return False, (
                f"Global rate limit exceeded: {self._rate_limits.max_calls_per_hour} calls/hour"
            )

        # Per-tool override
        per_tool_limit = self._rate_limits.per_tool_overrides.get(tool_name)
        if per_tool_limit and len(self._per_tool[tool_name]) >= per_tool_limit:
            return False, (
                f"Per-tool rate limit exceeded for {tool_name}: {per_tool_limit} calls/hour"
            )

        self._timestamps.append(now)
        self._per_tool[tool_name].append(now)
        return True, None


class PolicyMiddleware:
    """Evaluates IPC requests against a workspace security profile.

    Instantiated per-workspace so rate limit state is isolated.
    """

    def __init__(self, security: WorkspaceSecurity) -> None:
        self.security = security
        self.tracker: ActionTracker | None = None

        if security.rate_limits is not None:
            self.tracker = ActionTracker(security.rate_limits)

    def evaluate(self, tool_name: str, request: dict) -> PolicyDecision:
        """Evaluate whether a tool call should be allowed.

        Checks rate limits first, then tier-based policy.
        """
        # Rate limits apply to ALL tiers, even always-approve
        if self.tracker:
            allowed, reason = self.tracker.check_and_record(tool_name)
            if not allowed:
                return PolicyDecision(allowed=False, reason=reason)

        # Look up tool in security profile
        tool_config = self.security.mcp_tools.get(tool_name)

        if tool_config is not None:
            if not tool_config.enabled:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Tool '{tool_name}' is disabled in this workspace",
                )
            tier = tool_config.risk_tier
        else:
            # Tool not explicitly configured — use default tier
            tier = self.security.default_risk_tier

        # Evaluate by tier
        if tier == "always-approve":
            return PolicyDecision(allowed=True, reason="Auto-approved")

        if tier == "rules-engine":
            return self._apply_rules(tool_name, request)

        if tier == "human-approval":
            return PolicyDecision(
                allowed=False,
                reason="Requires human approval",
                requires_approval=True,
            )

        return PolicyDecision(allowed=False, reason=f"Unknown tier: {tier}")

    def _apply_rules(self, tool_name: str, request: dict) -> PolicyDecision:
        """Apply deterministic rules for rules-engine tier tools.

        Auto-approves all operations for now.
        Future: implement actual rules (e.g. "create_event only
        if calendar is user's own").
        """
        return PolicyDecision(
            allowed=True,
            reason="Rules-engine auto-approved (no rules configured yet)",
        )
