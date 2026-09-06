"""Host-side persistence for user-approved learned-skill decisions."""

from __future__ import annotations

from collections.abc import (
    Callable,
)
from typing import Any

_CHOICE_GRANTS = {
    "Grant always": True,
    "Deny always": False,
}


def persist_skill_access_choice(
    pending: dict[str, Any],
    answer: dict[str, Any],
    *,
    profile_name_for_group: Callable[[str], str],
    update_profile_skill_policy: Callable[..., None],
) -> str | None:
    """Persist a user's always-choice before the waiting agent receives it."""
    skill_name = _requested_skill_name(pending)
    choice = answer.get("answer")
    grant = _CHOICE_GRANTS.get(choice) if isinstance(choice, str) else None
    source_group = pending.get("source_group")
    if not skill_name or grant is None or not isinstance(source_group, str):
        return None

    update_profile_skill_policy(
        profile_name_for_group(source_group),
        skill_name,
        grant=grant,
    )
    return "granted" if grant else "denied"


def _requested_skill_name(pending: dict[str, Any]) -> str | None:
    questions = pending.get("questions")
    if not isinstance(questions, list):
        return None
    for question in questions:
        if not isinstance(question, dict):
            continue
        metadata = question.get("skill_access")
        skill_name = metadata.get("skill_name") if isinstance(metadata, dict) else None
        if isinstance(skill_name, str):
            return skill_name
    return None
