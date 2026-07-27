"""Tests for the Obsidian learning reviewer prompt and triage heuristics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.host.learning.packet_models import LearningPacket
from pynchy.host.learning.paths import LearningPaths
from pynchy.host.learning.reviewer import build_review_prompt, should_review

if TYPE_CHECKING:
    from pathlib import Path


def _packet(
    *,
    messages: list[dict[str, str]] | None = None,
    final_answer: str | None = "Done.",
    tool_counts: dict[str, int] | None = None,
    error_snippets: list[str] | None = None,
    loaded_skills: list[str] | None = None,
) -> LearningPacket:
    return LearningPacket(
        job_id="learning-1",
        chat_jid="slack:C123",
        group_folder="research",
        profile="Deep Work",
        created_at="2026-07-07T10:00:00+00:00",
        messages=messages or [{"role": "user", "content": "remember the workflow"}],
        final_answer=final_answer,
        tool_counts=tool_counts or {},
        error_snippets=error_snippets or [],
        loaded_skills=loaded_skills or [],
        provenance={"run_id": "run-123"},
    )


def _paths(tmp_path: Path) -> LearningPaths:
    vault_root = tmp_path / "vault"
    profile_root = vault_root / "systems/pynchy/profiles/deep-work"
    return LearningPaths(
        profile="Deep Work",
        profile_slug="deep-work",
        vault_root=vault_root,
        vault_mount_path="/workspace/vault",
        global_skills_root=vault_root / "systems/pynchy/skills",
        profile_root=profile_root,
        memory_root=profile_root / "memory",
        vault_mirror_root=tmp_path / "data" / "learning" / "vault-mirrors" / "deep-work",
        host_vault_mirror_root=tmp_path / "data" / "learning" / "host-vault-mirrors" / "deep-work",
        mounted_profile_root="/workspace/vault/systems/pynchy/profiles/deep-work",
        mounted_memory_root="/workspace/vault/systems/pynchy/profiles/deep-work/memory",
    )


def test_review_prompt_explains_memory_and_skill_placement(tmp_path: Path) -> None:
    prompt = build_review_prompt(_packet(), _paths(tmp_path))

    for snippet in (
        "/workspace/vault",
        "The mounted vault root is the global memory namespace.",
        "Use existing folder organization first.",
        (
            "Use the profile fallback memory path only when no repo, machine, subject, "
            "or other existing folder clearly fits."
        ),
        "Write learned skills only under the global skill registry.",
        "Do not invent semantic frontmatter requirements for memory notes.",
        (
            "Keep notes small and factual; update existing notes when that is cleaner "
            "than adding new ones."
        ),
        "If nothing durable was learned, make no filesystem changes.",
        "Profile fallback memory path: /workspace/vault/systems/pynchy/profiles/deep-work/memory",
        "Global skill registry: /workspace/vault/systems/pynchy/skills",
        "folder-governed",
        "Pynchy's existing `SKILL.md` skill format",
    ):
        assert snippet in prompt


def test_should_review_skips_short_casual_turn_without_learning_signal() -> None:
    packet = _packet(
        messages=[{"role": "user", "content": "thanks!"}],
        final_answer="You're welcome.",
        tool_counts={},
        error_snippets=[],
    )

    assert should_review(packet) is False


@pytest.mark.parametrize(
    "content",
    [
        "remember that this repo uses uvx ruff for linting",
        "learn this: deploys happen from mac-mini",
        "save this in memory for next time",
    ],
)
def test_should_review_accepts_explicit_learning_signals(content: str) -> None:
    assert should_review(_packet(messages=[{"role": "user", "content": content}])) is True


def test_should_review_accepts_tool_error_recovery() -> None:
    packet = _packet(
        messages=[{"role": "user", "content": "try the sync again"}],
        tool_counts={"shell": 2},
        error_snippets=["git fetch failed, then retry succeeded"],
    )

    assert should_review(packet) is True


def test_should_review_accepts_skill_worthy_repeated_workflow() -> None:
    packet = _packet(
        messages=[
            {
                "role": "user",
                "content": (
                    "Whenever I ask you to ship Pynchy, run uv run pytest and "
                    "uvx ruff check before committing."
                ),
            }
        ],
        final_answer="I'll follow that workflow.",
        tool_counts={},
        error_snippets=[],
    )

    assert should_review(packet) is True
