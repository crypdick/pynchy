"""Tests for the Obsidian learning reviewer prompt and triage heuristics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import configure_learning_paths_for, make_settings

from pynchy.config.api import LearningConfig, ObsidianLearningConfig, read_prompt
from pynchy.host.learning.api import run_learning_review
from pynchy.host.learning.paths import LearningPaths
from pynchy.host.learning.reviewer import build_review_prompt, should_review
from pynchy.learning_packets import LearningPacket


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
        vault_mount_path="/home/agent/memory",
        profile_root=profile_root,
        memory_root=profile_root / "memory",
        mounted_profile_root="/home/agent/memory/systems/pynchy/profiles/deep-work",
        mounted_memory_root="/home/agent/memory/systems/pynchy/profiles/deep-work/memory",
    )


def _reviewer_prompt() -> str:
    return read_prompt("reviewers/learning", Path(__file__).parents[1])


def test_review_prompt_explains_memory_and_skill_placement(tmp_path: Path) -> None:
    prompt = build_review_prompt(_packet(), _paths(tmp_path), _reviewer_prompt())
    normalized = " ".join(prompt.split())

    for snippet in (
        "/home/agent/memory",
        "The mounted vault root is the global memory namespace.",
        "Use existing folder organization first.",
        (
            "Use the profile fallback memory path only when no repo, machine, subject, "
            "or other existing folder clearly fits."
        ),
        "Create and update learned skills in the personalization skill registry.",
        "Do not invent semantic frontmatter requirements for memory notes.",
        (
            "Keep notes small and factual; update existing notes when that is cleaner "
            "than adding new ones."
        ),
        "If nothing durable was learned, make no filesystem changes.",
        "Profile fallback memory path: /home/agent/memory/systems/pynchy/profiles/deep-work/memory",
        "Personalization skill registry: /home/agent/skills",
        "Never author skills in a session `.claude/skills` or `.codex/skills` directory.",
        "folder-governed",
        "Pynchy's existing `SKILL.md` skill format",
    ):
        assert " ".join(snippet.split()) in normalized


def test_should_review_skips_short_casual_turn_without_learning_signal() -> None:
    packet = _packet(
        messages=[{"role": "user", "content": "thanks!"}],
        final_answer="You're welcome.",
        tool_counts={},
        error_snippets=[],
    )

    assert should_review(packet) is False


def test_should_review_skips_empty_packet_text() -> None:
    assert (
        should_review(_packet(messages=[{"role": "user", "content": ""}], final_answer=None))
        is False
    )


def test_should_review_skips_short_low_signal_turn_without_a_final_answer() -> None:
    packet = _packet(messages=[{"role": "user", "content": "thanks"}], final_answer=None)

    assert should_review(packet) is False


def test_should_review_accepts_tool_usage_without_an_error() -> None:
    packet = _packet(
        messages=[{"role": "user", "content": "check this"}],
        final_answer=None,
        tool_counts={"shell": 1},
    )

    assert should_review(packet) is True


def test_should_review_does_not_treat_long_casual_text_as_low_signal() -> None:
    packet = _packet(
        messages=[{"role": "user", "content": "thanks " * 25}],
        final_answer=None,
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


@pytest.mark.asyncio
async def test_hidden_learning_review_skips_low_signal_packet() -> None:
    run_agent = AsyncMock()

    result = await run_learning_review(
        _packet(messages=[{"role": "user", "content": "thanks!"}], final_answer="You're welcome."),
        run_agent,
        _reviewer_prompt(),
    )

    assert result == "skipped"
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_hidden_learning_review_runs_reviewer_with_a_scoped_workspace(tmp_path: Path) -> None:
    configure_learning_paths_for(
        make_settings(
            data_dir=tmp_path / "data",
            learning=LearningConfig(
                enabled=True,
                obsidian=ObsidianLearningConfig(vault_root=str(tmp_path / "vault")),
            ),
        )
    )

    async def successful_agent(*args, **kwargs) -> str:
        await kwargs["on_output"](object())
        return "success"

    run_agent = AsyncMock(side_effect=successful_agent)
    packet = _packet()
    result = await run_learning_review(packet, run_agent, _reviewer_prompt())

    assert result == "completed"
    workspace, reviewer_jid, messages = run_agent.await_args.args
    assert workspace.jid == reviewer_jid == "learning-review:deep-work"
    assert workspace.folder == "learning-review-deep-work"
    assert [message["role"] for message in messages] == ["user"]
    assert packet.messages[0]["content"] in messages[0]["content"]
    assert run_agent.await_args.kwargs["input_source"] == "hidden_learning_review"


@pytest.mark.asyncio
async def test_hidden_learning_review_rejects_unavailable_paths() -> None:
    with pytest.raises(RuntimeError, match="learning paths unavailable"):
        await run_learning_review(_packet(), AsyncMock(), _reviewer_prompt())


@pytest.mark.asyncio
async def test_hidden_learning_review_raises_when_reviewer_fails(tmp_path: Path) -> None:
    configure_learning_paths_for(
        make_settings(
            data_dir=tmp_path / "data",
            learning=LearningConfig(
                enabled=True,
                obsidian=ObsidianLearningConfig(vault_root=str(tmp_path / "vault")),
            ),
        )
    )

    with pytest.raises(RuntimeError, match="learning reviewer returned 'error'"):
        await run_learning_review(
            _packet(),
            AsyncMock(return_value="error"),
            _reviewer_prompt(),
        )
