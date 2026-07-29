"""Learned-skill access persistence boundary contracts."""

from __future__ import annotations

from pynchy.host.container_manager.ipc.skill_access import persist_skill_access_choice


def test_skill_access_choice_ignores_malformed_pending_questions() -> None:
    assert (
        persist_skill_access_choice(
            {"questions": "not-a-list"},
            {"answer": "Grant always"},
            profile_name_for_group=lambda _group: "profile",
            update_profile_skill_policy=lambda *_args, **_kwargs: None,
        )
        is None
    )


def test_skill_access_choice_skips_non_mapping_questions() -> None:
    assert (
        persist_skill_access_choice(
            {"questions": ["not-a-question"]},
            {"answer": "Grant always"},
            profile_name_for_group=lambda _group: "profile",
            update_profile_skill_policy=lambda *_args, **_kwargs: None,
        )
        is None
    )
