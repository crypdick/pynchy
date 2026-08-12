"""Semantic health classification for scheduled-work status projections."""

import re

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


def scheduled_work_health_reasons(
    *,
    status: str,
    next_run: str | None,
    consecutive_failures: int = 0,
    orchestration_error: str | None = None,
    last_result: str | None = None,
) -> tuple[str, ...]:
    """Return ordered, non-sensitive scheduler health reasons."""
    reasons: list[str] = []
    if status == "paused":
        reasons.append("paused")
    if status == "active" and not next_run:
        reasons.append("missing_next_run")
    if consecutive_failures > 0:
        reasons.append("recent_failure")
    if orchestration_error:
        reasons.append("scheduler_error")
    if _failure_shaped(last_result):
        reasons.append("failure_shaped_result")
    return tuple(reasons)


def _failure_shaped(value: str | None) -> bool:
    if value is None:
        return False
    text = _NEGATED_FAILURE.sub("", value)
    return any(pattern.search(text) for pattern in _FAILURE_PATTERNS)
