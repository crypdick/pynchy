"""Best-effort after-turn learning capture orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pynchy.agent_protocol.api import (
    ContainerOutput,
)
from pynchy.host.learning import packets as learning_packets
from pynchy.logger import logger
from pynchy.plugins.api import NewMessage
from pynchy.workspace.api import (
    WorkspaceProfile,
)

FetchMessagesSince = Callable[[str, str], Awaitable[list[NewMessage]]]
StartLearningReviewWorkflow = Callable[["LearningPacket"], Awaitable[None]]
LearningRunSummary = learning_packets.LearningRunSummary

if TYPE_CHECKING:
    from pynchy.learning_packets import LearningPacket


def is_after_turn_learning_enabled(*, enabled: bool, review_after_turn: bool) -> bool:
    return enabled and review_after_turn


def observe_learning_output(summary: LearningRunSummary, output: ContainerOutput) -> None:
    try:
        learning_packets.observe_container_output(summary, output)
    except Exception as exc:  # noqa: BLE001 - allow: exception-handling; learning must never block user output.
        logger.exception(
            "Failed to observe learning output",
            err=str(exc),
            output_type=output.type,
            status=output.status,
        )


async def messages_for_learning_packet(
    *,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    fetch_messages_since: FetchMessagesSince,
) -> list[NewMessage] | None:
    """Return the user turn covered by the final cursor for learning capture."""
    initial_last_timestamp = missed_messages[-1].timestamp
    if final_cursor <= initial_last_timestamp:
        return missed_messages

    try:
        expanded_messages = await fetch_messages_since(chat_jid, initial_last_timestamp)
    except Exception as exc:  # noqa: BLE001 - allow: exception-handling; learning fetch is best-effort.
        logger.exception(
            "Skipped learning packet because expanded message fetch failed",
            group=group.name,
            err=str(exc),
            final_cursor=final_cursor,
        )
        return None

    seen_ids: set[str] = set()
    covered_messages: list[NewMessage] = []
    for message in [*missed_messages, *expanded_messages]:
        if message.timestamp > final_cursor or message.id in seen_ids:
            continue
        seen_ids.add(message.id)
        covered_messages.append(message)

    # The last missed message is at or before final_cursor after the early return above,
    # so at least that message is always retained.
    return sorted(covered_messages, key=lambda message: message.timestamp)


async def start_completed_turn_learning_review(  # noqa: PLR0913 - learning review entry point mirrors the turn state it needs.
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
    fetch_messages_since: FetchMessagesSince,
    start_review_workflow: StartLearningReviewWorkflow,
    *,
    enabled: bool,
    review_after_turn: bool,
    packet_max_chars: int,
) -> str | None:
    if not is_after_turn_learning_enabled(
        enabled=enabled,
        review_after_turn=review_after_turn,
    ):
        return None

    try:
        logger.info(
            "Starting completed-turn learning capture",
            group=group.name,
            chat_jid=chat_jid,
            message_count=len(missed_messages),
            final_cursor=final_cursor,
        )
        learning_messages = await messages_for_learning_packet(
            chat_jid=chat_jid,
            group=group,
            missed_messages=missed_messages,
            final_cursor=final_cursor,
            fetch_messages_since=fetch_messages_since,
        )
        if learning_messages is None:
            return None
        job_id = await learning_packets.start_learning_review_workflow(
            chat_jid=chat_jid,
            group=group,
            missed_messages=learning_messages,
            final_cursor=final_cursor,
            summary=summary,
            enabled=enabled,
            packet_max_chars=packet_max_chars,
            start_review_workflow=start_review_workflow,
        )
    except Exception as exc:  # noqa: BLE001 - allow: exception-handling; learning must not fail user turns.
        logger.exception(
            "Failed to capture completed turn learning packet",
            group=group.name,
            err=str(exc),
        )
        return None
    else:
        logger.info(
            "Completed-turn learning capture finished",
            group=group.name,
            chat_jid=chat_jid,
            job_id=job_id,
        )
        return job_id
