"""Tests for bounded Obsidian learning packet construction."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.config.models import (
    LearningConfig,
    ObsidianLearningConfig,
    ProfileConfig,
    WorkspaceConfig,
)
from pynchy.host.learning.packet_codec import packet_from_payload, packet_to_payload
from pynchy.host.learning.packets import (
    LearningRunSummary,
    build_learning_packet,
    observe_container_output,
    packet_payload_char_limit,
    packet_to_reviewer_payload,
    start_learning_review_workflow,
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
        profiles={profile: ProfileConfig()},
        workspaces={"deep-work": WorkspaceConfig(profiles=[profile])},
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
    message_id: str = "msg-1",
    message_type: str = "user",
    sender: str = "user@example.com",
    sender_name: str = "Alice",
    timestamp: str = "2026-07-07T10:00:00Z",
) -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid="slack:C123",
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        message_type=message_type,
    )


def _serialized_reviewer_payload_chars(packet) -> int:
    payload = packet_to_reviewer_payload(packet)
    return len(json.dumps(payload, sort_keys=True))


def _pathological_messages(count: int = 30) -> list[NewMessage]:
    return [
        _message(
            f"long content {index} {'m' * 400}",
            message_id=f"message-{index}-{'i' * 400}",
            sender_name=f"Sender {index} {'s' * 400}",
            timestamp=f"2026-07-07T10:00:{index:02d}.000Z",
        )
        for index in range(count)
    ]


def _observe_pathological_tool_uses(
    summary: LearningRunSummary,
    *,
    redacted_value: str,
    count: int = 30,
) -> None:
    for index in range(count):
        observe_container_output(
            summary,
            ContainerOutput(
                status="success",
                type="tool_use",
                tool_name=f"VeryLongToolName-{index}-{'t' * 400}",
                tool_input={"command": f"echo {redacted_value}"},
            ),
        )


def _observe_pathological_tool_errors(
    summary: LearningRunSummary,
    count: int = 20,
) -> None:
    for index in range(count):
        observe_container_output(
            summary,
            ContainerOutput(
                status="success",
                type="tool_result",
                tool_result_is_error=True,
                tool_result_content=f"tool failed {index} {'e' * 400}",
            ),
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


def test_recovered_tool_result_errors_are_captured_without_tool_inputs() -> None:
    summary = LearningRunSummary()
    redacted_value = "redacted-tool-input-value"

    observe_container_output(
        summary,
        ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_is_error=True,
            tool_result_content="permission denied\x00while reading vault",
            tool_input={"command": f"cat {redacted_value}"},
        ),
    )
    for index in range(10):
        observe_container_output(
            summary,
            ContainerOutput(
                status="success",
                type="tool_result",
                tool_result_is_error=True,
                tool_result_content=f"recovered error {index}\x00 {'x' * 500}",
            ),
        )

    assert summary.error_snippets[0] == "permission denied while reading vault"
    assert redacted_value not in json.dumps(summary.error_snippets)
    assert len(summary.error_snippets) == 5
    assert all("\x00" not in snippet for snippet in summary.error_snippets)
    assert all(len(snippet) < 500 for snippet in summary.error_snippets)


def test_observe_container_output_only_treats_marked_tool_results_as_errors() -> None:
    summary = LearningRunSummary()

    observe_container_output(
        summary,
        ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_is_error=False,
            tool_result_content="recovered failure text",
        ),
    )
    observe_container_output(
        summary,
        ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_is_error=True,
            tool_result_content="marked failure text",
        ),
    )
    observe_container_output(
        summary,
        ContainerOutput(
            status="error",
            type="tool_result",
            tool_result_is_error=False,
            tool_result_content="status failure text",
        ),
    )

    assert summary.error_snippets == [
        "marked failure text",
        "status failure text",
    ]


def test_observe_container_output_does_not_treat_error_results_as_final_answers() -> None:
    summary = LearningRunSummary()

    observe_container_output(
        summary,
        ContainerOutput(
            status="error",
            type="result",
            result="command failed",
        ),
    )

    assert summary.final_answer is None
    assert summary.error_snippets == ["command failed"]


def test_build_packet_bounds_user_messages_and_skips_non_user_visible_messages(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=120)
    messages = [
        _message("this user message is much too long", message_id="msg-user"),
        _message("host only", message_id="msg-host", message_type="host"),
        _message("tool output", message_id="msg-tool", message_type="tool_result"),
        _message("notice", message_id="msg-notice", sender="system_notice"),
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
    assert len(packet.messages) == 1
    assert packet.messages[0]["role"] == "user"
    assert packet.messages[0]["sender_name"] == "Alice"
    assert packet.messages[0]["timestamp"] == "2026-07-07T10:00:00Z"
    assert packet.messages[0]["content"].startswith("this user")
    assert _serialized_reviewer_payload_chars(packet) <= packet_payload_char_limit(
        settings.learning.packet_max_chars
    )
    assert packet.provenance["source_message_ids"] == json.dumps(["msg-user"])


def test_build_packet_bounds_full_reviewer_payload_with_pathological_fields(
    tmp_path: Path,
) -> None:
    max_chars = 260
    long_profile = f"Research Profile {'p' * 900}"
    settings = _settings(tmp_path=tmp_path, packet_max_chars=max_chars, profile=long_profile)
    messages = _pathological_messages()
    summary = LearningRunSummary(final_answer=f"final answer {'a' * 900}")
    redacted_value = "redacted-tool-input-value"
    _observe_pathological_tool_uses(summary, redacted_value=redacted_value)
    _observe_pathological_tool_errors(summary)

    with _patch_learning_settings(settings):
        packet = build_learning_packet(
            chat_jid=f"slack:{'c' * 500}",
            group=_group(),
            missed_messages=messages,
            final_cursor=f"2026-07-07T10:00:29.000Z-{'z' * 500}",
            summary=summary,
        )

    assert packet is not None
    payload = packet_to_reviewer_payload(packet)
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert len(serialized_payload) <= packet_payload_char_limit(max_chars)
    assert redacted_value not in serialized_payload
    assert long_profile not in serialized_payload
    assert all(len(tool_name) < 400 for tool_name in packet.tool_counts)
    assert all(len(message["sender_name"]) < 400 for message in packet.messages)
    assert packet.messages


def test_build_packet_bounds_bursty_turn_as_one_packet(tmp_path: Path) -> None:
    max_chars = 240
    settings = _settings(tmp_path=tmp_path, packet_max_chars=max_chars)
    messages = [
        _message(
            "message body " * 80,
            message_id=f"msg-{index}-{'x' * 80}",
            sender_name=f"Sender {index} {'y' * 80}",
            timestamp=f"2026-07-07T10:00:{index:02d}Z",
        )
        for index in range(60)
    ]
    summary = LearningRunSummary(final_answer="final answer " * 80)
    for index in range(20):
        observe_container_output(
            summary,
            ContainerOutput(
                status="error",
                type="tool_result",
                error=f"tool error {index} {'z' * 120}",
            ),
        )

    with _patch_learning_settings(settings):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=messages,
            final_cursor="cursor-1",
            summary=summary,
        )

    assert packet is not None
    assert _serialized_reviewer_payload_chars(packet) <= packet_payload_char_limit(max_chars)
    assert len(packet.messages) < len(messages)
    assert len(packet.error_snippets) < len(summary.error_snippets)


def test_build_packet_skips_when_no_useful_message_content_can_be_captured(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=1)

    with _patch_learning_settings(settings):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("", sender_name="")],
            final_cursor="2026-07-07T10:00:00Z",
            summary=LearningRunSummary(),
        )

    assert packet is None


def test_build_packet_caps_final_answer_and_error_snippets(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=120)
    summary = LearningRunSummary(final_answer="assistant answer is long " * 20)
    observe_container_output(
        summary,
        ContainerOutput(status="error", type="result", error="error details are long " * 20),
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
    assert packet.final_answer is not None
    assert packet.final_answer.startswith("assistant answer")
    assert len(packet.final_answer) < len(summary.final_answer or "")
    assert len(packet.error_snippets) == 1
    assert packet.error_snippets[0].startswith("error details")
    assert _serialized_reviewer_payload_chars(packet) <= packet_payload_char_limit(
        settings.learning.packet_max_chars
    )


def test_tool_inputs_are_not_serialized_into_learning_packets(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=400)
    redacted_value = "redacted-tool-input-value"
    summary = LearningRunSummary(final_answer="Done")
    observe_container_output(
        summary,
        ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="Bash",
            tool_input={"command": f"curl -H 'Authorization: Bearer {redacted_value}'"},
        ),
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
    serialized_payload = json.dumps(packet_to_payload(packet), sort_keys=True)
    assert redacted_value not in serialized_payload
    assert "tool_input" not in serialized_payload
    assert packet.tool_counts == {"Bash": 1}


def _valid_packet_payload() -> dict[str, object]:
    return {
        "job_id": "job-1",
        "chat_jid": "slack:C123",
        "group_folder": "deep-work",
        "profile": "Deep Work",
        "created_at": "2026-07-07T10:00:00Z",
        "messages": [{"role": "user", "content": "remember this"}],
        "final_answer": "Done",
        "tool_counts": {"Bash": 1},
        "error_snippets": ["short error"],
        "loaded_skills": ["learning"],
        "provenance": {"source": "test"},
    }


class TestPacketCodecTypeChecks:
    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("chat_jid", 123, "must be a string"),
            ("final_answer", 123, "must be a string or null"),
            ("error_snippets", "nope", "must be a list"),
            ("messages", "nope", "must be a list"),
            ("provenance", "nope", "must be an object"),
            ("tool_counts", "nope", "must be an object"),
        ],
    )
    def test_top_level_type_checks_raise_typeerror(self, field, value, match):
        payload = _valid_packet_payload()
        payload[field] = value

        with pytest.raises(TypeError, match=match):
            packet_from_payload(payload)

    def test_required_int_dict_rejects_non_integer_values(self):
        payload = _valid_packet_payload()
        payload["tool_counts"] = {"count": "one"}

        with pytest.raises(TypeError, match="values must be integers"):
            packet_from_payload(payload)

    def test_required_int_dict_rejects_boolean_values(self):
        payload = _valid_packet_payload()
        payload["tool_counts"] = {"count": True}

        with pytest.raises(TypeError, match="values must be integers"):
            packet_from_payload(payload)

    def test_str_dict_from_mapping_rejects_non_string_items(self):
        payload = _valid_packet_payload()
        payload["provenance"] = {1: "one"}

        with pytest.raises(TypeError, match="keys must be strings"):
            packet_from_payload(payload)


def test_packet_provenance_and_profile_come_from_group_configuration(tmp_path: Path) -> None:
    settings = _settings(tmp_path=tmp_path, packet_max_chars=200, profile="Research Lab")
    messages = [
        _message("first", message_id="msg-1", timestamp="2026-07-07T10:00:00Z"),
        _message("second", message_id="msg-2", timestamp="2026-07-07T10:00:01Z"),
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


@pytest.mark.asyncio
async def test_learning_disabled_returns_no_packet_and_does_not_start_workflow(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path=tmp_path, enabled=False)

    with (
        _patch_learning_settings(settings),
        patch(
            "pynchy.host.orchestrator.temporal.scheduler.start_learning_review_workflow",
            new_callable=AsyncMock,
        ) as temporal_start,
    ):
        packet = build_learning_packet(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )
        job_id = await start_learning_review_workflow(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )

    assert packet is None
    assert job_id is None
    temporal_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_learning_review_workflow_starts_temporal_with_enabled_packet(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path=tmp_path)

    with (
        _patch_learning_settings(settings),
        patch(
            "pynchy.host.orchestrator.temporal.scheduler.start_learning_review_workflow",
            new_callable=AsyncMock,
        ) as temporal_start,
    ):
        job_id = await start_learning_review_workflow(
            chat_jid="slack:C123",
            group=_group(),
            missed_messages=[_message("remember this")],
            final_cursor="cursor-1",
            summary=LearningRunSummary(final_answer="Done"),
        )

    assert job_id is not None
    temporal_start.assert_awaited_once()
    packet = temporal_start.await_args.args[0]
    assert packet.job_id == job_id
    assert packet.final_answer == "Done"
