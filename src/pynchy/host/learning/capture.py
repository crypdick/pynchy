"""Best-effort after-turn learning capture orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from pynchy.config.settings import Settings
from pynchy.host.learning import packets as learning_packets
from pynchy.logger import logger
from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile

FetchMessagesSince = Callable[[str, str], Awaitable[list[NewMessage]]]
LearningRunSummary = learning_packets.LearningRunSummary


def is_after_turn_learning_enabled(settings: Settings) -> bool:
    return settings.learning.enabled and settings.learning.review_after_turn


def observe_learning_output(summary: LearningRunSummary, output: ContainerOutput) -> None:
    try:
        learning_packets.observe_container_output(summary, output)
    except Exception as exc:  # allow: exception-handling - learning must never block user output
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
    except Exception as exc:  # allow: exception-handling - learning fetch is best-effort
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

    if not covered_messages:
        logger.warning(
            "Expanded learning message fetch returned no covered messages",
            group=group.name,
            final_cursor=final_cursor,
        )
        return missed_messages

    return sorted(covered_messages, key=lambda message: message.timestamp)


async def enqueue_completed_turn_learning_packet(
    settings: Settings,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
    fetch_messages_since: FetchMessagesSince,
) -> Path | None:
    if not is_after_turn_learning_enabled(settings):
        return None

    try:
        learning_messages = await messages_for_learning_packet(
            chat_jid=chat_jid,
            group=group,
            missed_messages=missed_messages,
            final_cursor=final_cursor,
            fetch_messages_since=fetch_messages_since,
        )
        if learning_messages is None:
            return None
        return learning_packets.enqueue_learning_packet(
            chat_jid=chat_jid,
            group=group,
            missed_messages=learning_messages,
            final_cursor=final_cursor,
            summary=summary,
        )
    except Exception as exc:  # allow: exception-handling - learning must not fail user turns
        logger.exception(
            "Failed to capture completed turn learning packet",
            group=group.name,
            err=str(exc),
        )
        return None
