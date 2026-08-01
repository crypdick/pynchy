"""Public edge coverage for bounded learning-packet construction."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import configure_learning_paths_for, make_settings

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.config.api import LearningConfig, ObsidianLearningConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.learning.packets import (
    LearningRunSummary,
    build_learning_packet,
    observe_container_output,
    start_learning_review_workflow,
)
from pynchy.plugins.api import NewMessage
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _settings(*, tmp_path: Path, packet_max_chars: int = 40):
    vault = tmp_path / "vault"
    vault.mkdir()
    return make_settings(
        data_dir=tmp_path / "data",
        learning=LearningConfig(
            enabled=True,
            packet_max_chars=packet_max_chars,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        ),
        profiles={"Deep Work": ProfileConfig()},
        workspaces={"deep-work": WorkspaceConfig(profiles=["Deep Work"])},
    )


@contextmanager
def _configured(settings) -> Iterator[None]:
    configure_learning_paths_for(settings)
    yield


def _group() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="slack:C123", name="Deep Work", folder="deep-work", trigger="@pynchy"
    )


def _message(content: str, *, message_id: str = "msg-1", message_type: str = "user") -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid="slack:C123",
        sender="user@example.com",
        sender_name="Alice",
        content=content,
        timestamp="2026-07-07T10:00:00Z",
        message_type=message_type,
    )


def _build(settings, *, messages: list[NewMessage], summary: LearningRunSummary):
    return build_learning_packet(
        chat_jid="slack:C123",
        group=_group(),
        missed_messages=messages,
        final_cursor="cursor-1",
        summary=summary,
        enabled=True,
        packet_max_chars=settings.learning.packet_max_chars,
    )


def test_observe_container_output_ignores_blank_tool_names_and_errors() -> None:
    summary = LearningRunSummary()

    observe_container_output(
        summary,
        ContainerOutput(status="success", type="tool_use", tool_name=" \x00 "),
    )
    observe_container_output(
        summary,
        ContainerOutput(status="error", type="result", error="\x00\t"),
    )

    assert summary.tool_counts == {}
    assert summary.error_snippets == []


def test_build_skips_when_no_user_visible_messages_exist(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)

    with _configured(settings):
        packet = _build(
            settings,
            messages=[_message("host", message_type="host")],
            summary=LearningRunSummary(),
        )

    assert packet is None


def test_build_skips_packet_that_cannot_fit_fixed_payload_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)

    with (
        _configured(settings),
        patch(
            "pynchy.host.learning.packets._serialized_payload_chars",
            return_value=999_999,
        ),
    ):
        packet = _build(
            settings,
            messages=[_message("remember this")],
            summary=LearningRunSummary(),
        )

    assert packet is None


def test_build_skips_when_learning_path_resolution_returns_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)

    with (
        _configured(settings),
        patch("pynchy.host.learning.packets.resolve_learning_paths", return_value=None),
    ):
        packet = _build(
            settings, messages=[_message("remember this")], summary=LearningRunSummary()
        )

    assert packet is None


def test_build_skips_when_learning_path_resolution_fails(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)

    with (
        _configured(settings),
        patch(
            "pynchy.host.learning.packets.resolve_learning_paths",
            side_effect=ValueError("bad paths"),
        ),
    ):
        packet = _build(
            settings, messages=[_message("remember this")], summary=LearningRunSummary()
        )

    assert packet is None


def test_build_allows_a_summary_without_a_final_answer(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)

    with _configured(settings):
        packet = _build(
            settings, messages=[_message("remember this")], summary=LearningRunSummary()
        )

    assert packet is not None
    assert packet.final_answer is None


def test_build_caps_source_ids_with_a_tiny_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=4)

    with _configured(settings):
        packet = _build(
            settings,
            messages=[_message("x", message_id="message-id")],
            summary=LearningRunSummary(),
        )

    assert packet is not None
    assert packet.provenance["source_message_ids"] == json.dumps([])


def test_build_with_one_character_budget_omits_source_ids(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=1)

    with _configured(settings):
        packet = _build(
            settings,
            messages=[_message("x", message_id="message-id")],
            summary=LearningRunSummary(),
        )

    assert packet is not None
    assert packet.messages[0]["content"] == "x"
    assert not packet.provenance["source_message_ids"]


def test_build_skips_blank_and_bounds_error_snippets(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=40)

    with _configured(settings):
        packet = _build(
            settings,
            messages=[_message("remember this")],
            summary=LearningRunSummary(error_snippets=["\x00", "first", "second " + "x" * 100]),
        )

    assert packet is not None
    assert packet.error_snippets == ["first", "sec"]


def test_build_bounds_tool_counts_without_empty_names(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)

    with _configured(settings):
        packet = _build(
            settings,
            messages=[_message("remember this")],
            summary=LearningRunSummary(tool_counts={" \x00 ": 1, "Bash": 2}),
        )

    assert packet is not None
    assert packet.tool_counts == {"Bash": 2}


@pytest.mark.asyncio
async def test_start_learning_review_workflow_swallows_workflow_start_failure(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path=tmp_path)
    start_review_workflow = AsyncMock(side_effect=RuntimeError("reviewer unavailable"))

    with _configured(settings):
        job_id = await start_learning_review_workflow(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
            enabled=True,
            packet_max_chars=settings.learning.packet_max_chars,
            start_review_workflow=start_review_workflow,
        )

    assert job_id is None
    start_review_workflow.assert_awaited_once()
