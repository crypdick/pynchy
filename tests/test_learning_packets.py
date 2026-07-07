"""Tests for bounded Obsidian learning packet construction."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from conftest import make_settings

from pynchy.config.models import LearningConfig, ObsidianLearningConfig, WorkspaceConfig
from pynchy.host.learning.packets import (
    LearningRunSummary,
    build_learning_packet,
    enqueue_learning_packet,
    observe_container_output,
)
from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile


def _settings(
    *,
    tmp_path: Path,
    enabled: bool = True,
    packet_max_chars: int = 40,
    profile: str = "Deep Work",
):
    vault = tmp_path / "vault"
    vault.mkdir()
    return make_settings(
        data_dir=tmp_path / "data",
        learning=LearningConfig(
            enabled=enabled,
            packet_max_chars=packet_max_chars,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        ),
        workspaces={"deep-work": WorkspaceConfig(profile=profile)},
    )


@contextmanager
def _patch_learning_settings(settings) -> Iterator[None]:
    with (
        patch("pynchy.host.learning.paths.get_settings", return_value=settings),
        patch("pynchy.host.learning.packets.get_settings", return_value=settings),
    ):
        yield


def _group() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="slack:C123",
        name="Deep Work",
        folder="deep-work",
        trigger="@pynchy",
    )


def _message(
    content: str,
    *,
    id: str = "msg-1",
    message_type: str = "user",
    sender: str = "user@example.com",
    sender_name: str = "Alice",
    timestamp: str = "2026-07-07T10:00:00Z",
) -> NewMessage:
    return NewMessage(
        id=id,
        chat_jid="slack:C123",
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        message_type=message_type,
    )


def test_observe_container_output_records_final_answer_and_tool_counts() -> None:
    summary = LearningRunSummary()

    observe_container_output(
        summary,
        ContainerOutput(status="success", type="result", result="Final answer"),
    )
    observe_container_output(
        summary,
        ContainerOutput(status="success", type="tool_use", tool_name="Bash"),
    )
    observe_container_output(
        summary,
        ContainerOutput(status="success", type="tool_use", tool_name="Bash"),
    )
    observe_container_output(
        summary,
        ContainerOutput(status="success", type="tool_use", tool_name="Read"),
    )

    assert summary.final_answer == "Final answer"
    assert summary.tool_counts == {"Bash": 2, "Read": 1}


def test_build_packet_caps_user_messages_and_skips_non_user_visible_messages(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=12)
    messages = [
        _message("this user message is much too long", id="msg-user"),
        _message("host only", id="msg-host", message_type="host"),
        _message("tool output", id="msg-tool", message_type="tool_result"),
        _message("notice", id="msg-notice", sender="system_notice"),
    ]

    with _patch_learning_settings(settings):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=messages,
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )

    assert packet is not None
    assert packet.messages == [
        {
            "role": "user",
            "sender_name": "Alice",
            "timestamp": "2026-07-07T10:00:00Z",
            "content": "this user...",
        }
    ]
    assert packet.provenance["source_message_ids"] == json.dumps(["msg-user"])


def test_build_packet_caps_final_answer_and_error_snippets(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=12)
    summary = LearningRunSummary(final_answer="assistant answer is long")
    observe_container_output(
        summary,
        ContainerOutput(status="error", type="result", error="error details are long"),
    )

    with _patch_learning_settings(settings):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=summary,
        )

    assert packet is not None
    assert packet.final_answer == "assistant..."
    assert packet.error_snippets == ["error det..."]


def test_packet_provenance_and_profile_come_from_group_configuration(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, profile="Research Lab")
    messages = [
        _message("first", id="msg-1", timestamp="2026-07-07T10:00:00Z"),
        _message("second", id="msg-2", timestamp="2026-07-07T10:00:01Z"),
    ]

    with _patch_learning_settings(settings):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=messages,
            final_cursor="2026-07-07T10:00:01Z",
            summary=LearningRunSummary(final_answer="Done"),
        )

    assert packet is not None
    assert packet.chat_jid == "slack:C123"
    assert packet.group_folder == "deep-work"
    assert packet.profile == "Research Lab"
    assert packet.provenance == {
        "chat_jid": "slack:C123",
        "group_folder": "deep-work",
        "final_cursor": "2026-07-07T10:00:01Z",
        "source_message_ids": json.dumps(["msg-1", "msg-2"]),
    }


def test_learning_disabled_returns_no_packet_and_does_not_enqueue(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, enabled=False)

    with (
        _patch_learning_settings(settings),
        patch("pynchy.host.learning.packets.LearningQueue") as queue_cls,
    ):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )
        path = enqueue_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )

    assert packet is None
    assert path is None
    queue_cls.assert_not_called()


def test_enqueue_learning_packet_writes_enabled_packet(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path)
    queue_path = tmp_path / "data" / "ipc" / "learning" / "pending" / "packet.json"

    with (
        _patch_learning_settings(settings),
        patch("pynchy.host.learning.packets.LearningQueue") as queue_cls,
    ):
        queue_cls.return_value.enqueue.return_value = queue_path
        path = enqueue_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )

    assert path == queue_path
    queue_cls.assert_called_once_with()
    packet = queue_cls.return_value.enqueue.call_args.args[0]
    assert packet.final_answer == "Done"
