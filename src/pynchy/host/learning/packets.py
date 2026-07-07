"""Construct bounded learning packets from completed user turns."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pynchy.config import get_settings
from pynchy.host.learning.paths import (
    LearningConfigError,
    resolve_learning_paths,
)
from pynchy.host.learning.queue import LearningPacket, LearningQueue
from pynchy.logger import logger
from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile

_MAX_PACKET_MESSAGES = 20
_MAX_ERROR_SNIPPETS = 5
_MAX_TOOL_COUNT_ENTRIES = 40
_MIN_SOURCE_IDS_CHARS = 2


@dataclass
class LearningRunSummary:
    final_answer: str | None = None
    tool_counts: dict[str, int] = field(default_factory=dict)
    error_snippets: list[str] = field(default_factory=list)


def observe_container_output(summary: LearningRunSummary, output: ContainerOutput) -> None:
    if output.type == "result" and output.result is not None:
        summary.final_answer = output.result

    if output.type == "tool_use" and output.tool_name:
        summary.tool_counts[output.tool_name] = summary.tool_counts.get(output.tool_name, 0) + 1

    if output.status == "error":
        error_text = output.error or output.result or output.text or output.tool_result_content
        if error_text:
            summary.error_snippets.append(error_text)


def build_learning_packet(
    *,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
) -> LearningPacket | None:
    settings = get_settings()
    if not settings.learning.enabled:
        return None

    try:
        paths = resolve_learning_paths(group.folder)
    except (LearningConfigError, ValueError):
        return None
    if paths is None:
        return None

    max_chars = settings.learning.packet_max_chars
    user_messages = [
        message for message in missed_messages if _is_user_visible_user_message(message)
    ]
    if not user_messages:
        return None
    nonempty_error_snippets = [snippet for snippet in summary.error_snippets if snippet]
    budgets = _packet_budgets(
        max_chars,
        has_final_answer=summary.final_answer is not None,
        has_error_snippets=bool(nonempty_error_snippets),
    )
    messages, captured_messages = _bounded_messages(user_messages, budgets.messages)

    now = datetime.now(UTC)
    return LearningPacket(
        job_id=_new_job_id(now),
        chat_jid=chat_jid,
        group_folder=group.folder,
        profile=paths.profile,
        created_at=now.isoformat(),
        messages=messages,
        final_answer=_cap_optional_text(summary.final_answer, budgets.final_answer),
        tool_counts=_bounded_tool_counts(summary.tool_counts),
        error_snippets=_bounded_error_snippets(nonempty_error_snippets, budgets.error_snippets),
        loaded_skills=[],
        provenance={
            "chat_jid": chat_jid,
            "group_folder": group.folder,
            "final_cursor": final_cursor,
            "source_message_ids": _bounded_source_message_ids(
                captured_messages, budgets.source_ids
            ),
        },
    )


def enqueue_learning_packet(
    *,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
) -> Path | None:
    try:
        packet = build_learning_packet(
            chat_jid=chat_jid,
            group=group,
            missed_messages=missed_messages,
            final_cursor=final_cursor,
            summary=summary,
        )
        if packet is None:
            return None
        return LearningQueue().enqueue(packet)
    except Exception as exc:  # allow: exception-handling — learning must not fail user turns
        logger.exception(
            "Failed to enqueue learning packet",
            group=group.name,
            err=str(exc),
        )
        return None


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
        capped = _cap_text(value, self.remaining)
        self.remaining -= len(capped)
        return capped


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
        if deficit == 0:
            return


def _bounded_messages(
    user_messages: list[NewMessage], max_chars: int
) -> tuple[list[dict[str, str]], list[NewMessage]]:
    budget = _TextBudget(max_chars)
    messages: list[dict[str, str]] = []
    captured_messages: list[NewMessage] = []
    for message in user_messages[:_MAX_PACKET_MESSAGES]:
        if budget.remaining <= 0 and messages:
            break
        messages.append(
            {
                "role": "user",
                "sender_name": budget.take(message.sender_name),
                "timestamp": message.timestamp,
                "content": budget.take(message.content),
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
    return dict(sorted(tool_counts.items())[:_MAX_TOOL_COUNT_ENTRIES])


def _bounded_source_message_ids(messages: list[NewMessage], max_chars: int) -> str:
    if max_chars < _MIN_SOURCE_IDS_CHARS:
        return ""

    ids: list[str] = []
    for message in messages:
        candidate = json.dumps([*ids, message.id])
        if len(candidate) > max_chars:
            break
        ids.append(message.id)
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


def _new_job_id(now: datetime) -> str:
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    return f"learning-{timestamp}-{uuid4().hex[:12]}"
