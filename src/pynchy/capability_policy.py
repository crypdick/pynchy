"""Shared capability-pattern intersection rules."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves helper annotations.
    Iterable,
)

from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves helper annotations.
    CapabilityRule,
)

_DECISION_RANK = {"allow": 0, "needs_human": 1, "deny": 2}


def capability_pattern_matches(pattern: str, capability: str) -> bool:
    """Return whether an exact capability is covered by a trailing wildcard."""
    if pattern == capability:
        return True
    if not pattern.endswith(".*"):
        return False
    prefix = pattern[:-2]
    return bool(prefix) and capability.startswith(f"{prefix}.")


def most_restrictive_capability_rule(
    rules: Iterable[CapabilityRule],
) -> CapabilityRule | None:
    """Intersect matching rules by selecting the most restrictive decision."""
    return max(rules, key=lambda rule: _DECISION_RANK[rule.decision], default=None)
