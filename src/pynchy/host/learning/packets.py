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
    profile_name_for_group,
    resolve_learning_paths,
)
from pynchy.host.learning.queue import LearningPacket, LearningQueue
from pynchy.logger import logger
from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile


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
        profile = profile_name_for_group(group.folder)
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

    now = datetime.now(UTC)
    return LearningPacket(
        job_id=_new_job_id(now),
        chat_jid=chat_jid,
        group_folder=group.folder,
        profile=profile,
        created_at=now.isoformat(),
        messages=[
            {
                "role": "user",
                "sender_name": message.sender_name,
                "timestamp": message.timestamp,
                "content": _cap_text(message.content, max_chars),
            }
            for message in user_messages
        ],
        final_answer=_cap_optional_text(summary.final_answer, max_chars),
        tool_counts=dict(sorted(summary.tool_counts.items())),
        error_snippets=[
            _cap_text(snippet, max_chars) for snippet in summary.error_snippets if snippet
        ],
        loaded_skills=[],
        provenance={
            "chat_jid": chat_jid,
            "group_folder": group.folder,
            "final_cursor": final_cursor,
            "source_message_ids": json.dumps([message.id for message in user_messages]),
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
