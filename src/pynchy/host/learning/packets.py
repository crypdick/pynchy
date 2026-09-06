"""Construct bounded learning packets from completed user turns."""

from __future__ import annotations

import json
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from pynchy.agent_protocol.api import (
    ContainerOutput,
)
from pynchy.host.learning.paths import (
    LearningConfigError,
    resolve_learning_paths,
)
from pynchy.learning_packets import LearningPacket
from pynchy.learning_packets import packet_to_payload as _packet_to_payload
from pynchy.logger import logger
from pynchy.plugins.api import (
    NewMessage,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

_MAX_PACKET_MESSAGES = 8
_MAX_ERROR_SNIPPETS = 5
_MAX_TOOL_COUNT_ENTRIES = 8
_MIN_SOURCE_IDS_CHARS = 2
_PACKET_PAYLOAD_OVERHEAD_CHARS = 2_048
_MAX_METADATA_CHARS = 96
_MAX_TIMESTAMP_CHARS = 40
_MAX_TOOL_NAME_CHARS = 48
_MAX_ERROR_SNIPPET_CHARS = 240
_MAX_TOOL_COUNT_VALUE = 999


@dataclass
class LearningRunSummary:
    final_answer: str | None = None
    tool_counts: dict[str, int] = field(default_factory=dict)
    error_snippets: list[str] = field(default_factory=list)


def packet_to_reviewer_payload(packet: LearningPacket) -> dict[str, Any]:
    """Return the exact JSON payload shape consumed by the Temporal reviewer."""
    return _packet_to_payload(packet)


def packet_payload_char_limit(packet_max_chars: int) -> int:
    """Maximum serialized packet payload size, including fixed JSON metadata."""
    return max(0, packet_max_chars) + _PACKET_PAYLOAD_OVERHEAD_CHARS


def observe_container_output(summary: LearningRunSummary, output: ContainerOutput) -> None:
    if _is_successful_result(output):
        summary.final_answer = _sanitize_text(cast("str", output.result))

    if output.type == "tool_use" and output.tool_name:
        _record_tool_count(summary, output.tool_name)

    error_text = _error_text(output)
    if error_text is not None:
        _append_error_snippet(summary, error_text)
        return

    # Cores must mark recovered tool failures with tool_result_is_error=True
    # or they are indistinguishable from successful tool output here.
    if _is_recovered_tool_error(output):
        _append_error_snippet(summary, cast("str", output.tool_result_content))


def build_learning_packet(  # noqa: PLR0913 - packet construction receives values resolved at composition.
    *,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
    enabled: bool,
    packet_max_chars: int,
) -> LearningPacket | None:
    profile = _resolve_learning_profile(
        enabled=enabled,
        group_folder=group.folder,
    )
    if profile is None:
        return None
    prepared = _prepare_packet_content(
        missed_messages=missed_messages,
        summary=summary,
        max_chars=packet_max_chars,
    )
    if prepared is None:
        return None

    now = datetime.now(UTC)
    packet = LearningPacket(
        job_id=_new_job_id(now),
        chat_jid=_bounded_metadata(chat_jid),
        group_folder=_bounded_metadata(group.folder),
        profile=_bounded_metadata(profile),
        created_at=now.isoformat(),
        messages=prepared.messages,
        final_answer=_cap_optional_text(summary.final_answer, prepared.budgets.final_answer),
        tool_counts=_bounded_tool_counts(summary.tool_counts),
        error_snippets=_bounded_error_snippets(
            prepared.error_snippets,
            prepared.budgets.error_snippets,
        ),
        loaded_skills=[],
        provenance={
            "chat_jid": _bounded_metadata(chat_jid),
            "group_folder": _bounded_metadata(group.folder),
            "final_cursor": _cap_text(_sanitize_text(final_cursor), _MAX_TIMESTAMP_CHARS),
            "source_message_ids": _bounded_source_message_ids(
                prepared.captured_messages, prepared.budgets.source_ids
            ),
        },
    )
    if not _fits_packet_budget(packet, packet_max_chars, group.name):
        return None
    return packet


async def start_learning_review_workflow(  # noqa: PLR0913 - workflow launch receives the packet's resolved limits.
    *,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
    enabled: bool,
    packet_max_chars: int,
    start_review_workflow: Callable[[LearningPacket], Awaitable[None]],
) -> str | None:
    try:
        packet = build_learning_packet(
            chat_jid=chat_jid,
            group=group,
            missed_messages=missed_messages,
            final_cursor=final_cursor,
            summary=summary,
            enabled=enabled,
            packet_max_chars=packet_max_chars,
        )
        if packet is None:
            return None
        await start_review_workflow(packet)
    except Exception as exc:  # noqa: BLE001 - allow: exception-handling; learning must not fail user turns.
        logger.exception(
            "Failed to start learning review workflow",
            group=group.name,
            err=str(exc),
        )
        return None
    else:
        return packet.job_id


def _is_user_visible_user_message(message: NewMessage) -> bool:
    return message.message_type == "user" and message.sender != "system_notice"


@dataclass(frozen=True)
class _PacketBudgets:
    messages: int
    final_answer: int
    error_snippets: int
    source_ids: int


@dataclass
class _TextBudget:
    remaining: int

    def take(self, value: str) -> str:
        if self.remaining <= 0:
            return ""
        capped = _cap_text(_sanitize_text(value), self.remaining)
        self.remaining -= len(capped)
        return capped


@dataclass(frozen=True)
class _PreparedPacketContent:
    messages: list[dict[str, str]]
    captured_messages: list[NewMessage]
    error_snippets: list[str]
    budgets: _PacketBudgets


def _is_successful_result(output: ContainerOutput) -> bool:
    return output.status != "error" and output.type == "result" and output.result is not None


def _error_text(output: ContainerOutput) -> str | None:
    if output.status != "error":
        return None
    return output.error or output.result or output.text or output.tool_result_content


def _is_recovered_tool_error(output: ContainerOutput) -> bool:
    return (
        output.type == "tool_result"
        and output.tool_result_is_error is True
        and output.tool_result_content is not None
    )


def _resolve_learning_profile(*, enabled: bool, group_folder: str) -> str | None:
    if not enabled:
        return None

    try:
        paths = resolve_learning_paths(group_folder)
    except (LearningConfigError, ValueError):
        return None
    if paths is None:
        return None
    return paths.profile


def _prepare_packet_content(
    *,
    missed_messages: list[NewMessage],
    summary: LearningRunSummary,
    max_chars: int,
) -> _PreparedPacketContent | None:
    user_messages = [
        message for message in missed_messages if _is_user_visible_user_message(message)
    ]
    if not user_messages:
        return None

    error_snippets = [snippet for snippet in summary.error_snippets if snippet]
    budgets = _packet_budgets(
        max_chars,
        has_final_answer=summary.final_answer is not None,
        has_error_snippets=bool(error_snippets),
    )
    messages, captured_messages = _bounded_messages(user_messages, budgets.messages)
    if not messages:
        return None
    return _PreparedPacketContent(
        messages=messages,
        captured_messages=captured_messages,
        error_snippets=error_snippets,
        budgets=budgets,
    )


def _fits_packet_budget(packet: LearningPacket, max_chars: int, group_name: str) -> bool:
    payload_chars = _serialized_payload_chars(packet)
    limit = packet_payload_char_limit(max_chars)
    if payload_chars <= limit:
        return True

    logger.warning(
        "Skipped oversized learning packet after bounding",
        group=group_name,
        payload_chars=payload_chars,
        limit=limit,
    )
    return False


def _packet_budgets(
    max_chars: int,
    *,
    has_final_answer: bool,
    has_error_snippets: bool,
) -> _PacketBudgets:
    sections = [("messages", 6)]
    if has_final_answer:
        sections.append(("final_answer", 3))
    if has_error_snippets:
        sections.append(("error_snippets", 2))
    sections.append(("source_ids", 1))

    total_weight = sum(weight for _, weight in sections)
    raw_budgets = {
        "messages": 0,
        "final_answer": 0,
        "error_snippets": 0,
        "source_ids": 0,
    }
    used = 0
    for name, weight in sections:
        value = max_chars * weight // total_weight
        raw_budgets[name] = value
        used += value
    raw_budgets[sections[0][0]] += max_chars - used
    _reserve_source_id_budget(raw_budgets, max_chars)
    return _PacketBudgets(
        messages=raw_budgets["messages"],
        final_answer=raw_budgets["final_answer"],
        error_snippets=raw_budgets["error_snippets"],
        source_ids=raw_budgets["source_ids"],
    )


def _reserve_source_id_budget(raw_budgets: dict[str, int], max_chars: int) -> None:
    minimum = _MIN_SOURCE_IDS_CHARS if max_chars >= _MIN_SOURCE_IDS_CHARS else 0
    deficit = minimum - raw_budgets["source_ids"]
    if deficit <= 0:
        return

    for donor in ("messages", "final_answer", "error_snippets"):
        taken = min(deficit, raw_budgets[donor])
        raw_budgets[donor] -= taken
        raw_budgets["source_ids"] += taken
        deficit -= taken


def _bounded_messages(
    user_messages: list[NewMessage], max_chars: int
) -> tuple[list[dict[str, str]], list[NewMessage]]:
    budget = _TextBudget(max_chars)
    messages: list[dict[str, str]] = []
    captured_messages: list[NewMessage] = []
    for message in user_messages[:_MAX_PACKET_MESSAGES]:
        if budget.remaining <= 0:
            break
        content = budget.take(message.content)
        if not content:
            continue
        messages.append(
            {
                "role": "user",
                "sender_name": budget.take(message.sender_name),
                "timestamp": _cap_text(_sanitize_text(message.timestamp), _MAX_TIMESTAMP_CHARS),
                "content": content,
            }
        )
        captured_messages.append(message)
    return messages, captured_messages


def _bounded_error_snippets(snippets: list[str], max_chars: int) -> list[str]:
    budget = _TextBudget(max_chars)
    bounded: list[str] = []
    for snippet in snippets[:_MAX_ERROR_SNIPPETS]:
        capped = budget.take(snippet)
        if capped:
            bounded.append(capped)
        if budget.remaining <= 0:
            break
    return bounded


def _bounded_tool_counts(tool_counts: dict[str, int]) -> dict[str, int]:
    bounded: dict[str, int] = {}
    for raw_name, count in sorted(tool_counts.items()):
        name = _cap_text(_sanitize_text(raw_name), _MAX_TOOL_NAME_CHARS)
        if not name:
            continue
        bounded[name] = min(
            bounded.get(name, 0) + count,
            _MAX_TOOL_COUNT_VALUE,
        )
        if len(bounded) >= _MAX_TOOL_COUNT_ENTRIES:
            break
    return bounded


def _bounded_source_message_ids(messages: list[NewMessage], max_chars: int) -> str:
    if max_chars < _MIN_SOURCE_IDS_CHARS:
        return ""

    ids: list[str] = []
    for message in messages:
        message_id = _cap_text(_sanitize_text(message.id), max_chars)
        candidate = json.dumps([*ids, message_id])
        if len(candidate) > max_chars:
            break
        ids.append(message_id)
    return json.dumps(ids)


def _cap_optional_text(value: str | None, max_chars: int) -> str | None:
    if value is None:
        return None
    return _cap_text(value, max_chars)


def _cap_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return f"{value[: max_chars - 3]}..."


def _sanitize_text(value: str) -> str:
    return "".join(_safe_char(char) for char in value).strip()


def _safe_char(char: str) -> str:
    if char in {"\n", "\t"} or ord(char) >= 32:
        return char
    return " "


def _record_tool_count(summary: LearningRunSummary, raw_tool_name: str) -> None:
    tool_name = _cap_text(_sanitize_text(raw_tool_name), _MAX_TOOL_NAME_CHARS)
    if not tool_name:
        return
    if tool_name not in summary.tool_counts and len(summary.tool_counts) >= _MAX_TOOL_COUNT_ENTRIES:
        return
    summary.tool_counts[tool_name] = min(
        summary.tool_counts.get(tool_name, 0) + 1,
        _MAX_TOOL_COUNT_VALUE,
    )


def _append_error_snippet(summary: LearningRunSummary, raw_error: str) -> None:
    if len(summary.error_snippets) >= _MAX_ERROR_SNIPPETS:
        return
    snippet = _cap_text(_sanitize_text(raw_error), _MAX_ERROR_SNIPPET_CHARS)
    if snippet:
        summary.error_snippets.append(snippet)


def _bounded_metadata(value: str) -> str:
    return _cap_text(_sanitize_text(value), _MAX_METADATA_CHARS)


def _serialized_payload_chars(packet: LearningPacket) -> int:
    return len(json.dumps(packet_to_reviewer_payload(packet), sort_keys=True))


def _new_job_id(now: datetime) -> str:
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    return f"learning-{timestamp}-{uuid4().hex[:12]}"
