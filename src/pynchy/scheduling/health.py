"""Semantic health classification for scheduled-work status projections."""

from __future__ import annotations

import re
from dataclasses import dataclass

ScheduledWorkAttention = tuple[
    str, ...
]  # Stable reason codes safe to expose outside the host process.


@dataclass(frozen=True)
class ScheduledWorkHealth:
    """Host evidence for one scheduled-work definition."""

    status: str
    next_run: str | None
    last_run_status: str | None
    consecutive_failures: int
    orchestration_error: str | None
    last_result: str | None


_NEGATED_FAILURE = re.compile(
    r"\b(?:"
    r"(?:no|zero|0)\s+(?:errors?|failures?|blockers?)"
    r"(?:\s+(?:or|and)\s+(?:errors?|failures?|blockers?))*"
    r"|no\s+failed\s+\w+"
    r"|not\s+(?:blocked|failed|failing)"
    r"|without\s+(?:any\s+)?(?:errors?|failures?|blockers?)"
    r"(?:\s+(?:or|and)\s+(?:errors?|failures?|blockers?))*"
    r")\b",
    re.IGNORECASE,
)
_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bblocked\b",
        r"\berrors?\b",
        r"\bfail(?:ed|ure|ures|ing)?\b",
        r"\b(?:unable|unavailable)\b",
        r"\bcould not\b",
        r"\bmissing\s+(?:a\s+)?(?:credentials?|token|api[ -]?key|secret)\b",
        r"\bpermission denied\b",
        r"\bforbidden\b",
        r"\blogin required\b",
        r"\bconnection refused\b",
        r"\bnot configured\b",
        r"\bneeds? setup\b",
        r"\brate[ -]?limit(?:ed|ing)?\b",
        r"\bunauthorized\b",
        r"\btim(?:e|ed) out\b",
        r"\btimeout\b",
    )
)


def scheduled_work_attention(health: ScheduledWorkHealth) -> ScheduledWorkAttention:
    """Return ordered, non-sensitive reasons requiring scheduler attention."""
    reasons: list[str] = []
    if health.status == "paused":
        reasons.append("paused")
    if health.status == "active" and not health.next_run:
        reasons.append("missing_next_run")
    if health.last_run_status == "error" or health.consecutive_failures > 0:
        reasons.append("recent_failure")
    if health.orchestration_error:
        reasons.append("scheduler_error")
    if _failure_shaped(health.last_result):
        reasons.append("failure_shaped_result")
    return tuple(reasons)


def _failure_shaped(value: str | None) -> bool:
    if value is None:
        return False
    text = _NEGATED_FAILURE.sub("", value)
    return any(pattern.search(text) for pattern in _FAILURE_PATTERNS)
