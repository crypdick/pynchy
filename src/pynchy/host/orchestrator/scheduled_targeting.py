"""Reservation policy for direct scheduled-task conversations."""

from __future__ import annotations

from pynchy.host.container_manager import get_session
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.types import (
    GroupFolder,
    InFlightTurn,
    InFlightWorkKind,
    ScheduledTask,
)


class ScheduledTargetBusyError(RuntimeError):
    """A reserved scheduled target cannot provide an isolated child conversation."""


def base_channel_is_reserved(task: ScheduledTask, turns: list[InFlightTurn]) -> bool:
    session = get_session(GroupFolder(task.group_folder))
    session_is_live = session is not None and session.is_alive
    return session_is_live or any(turn.chat_jid == task.chat_jid for turn in turns)


def numbered_slot_is_reserved(
    task: ScheduledTask,
    slot: int,
    turns: list[InFlightTurn],
) -> bool:
    return any(
        turn.scheduled_base_chat_jid == task.chat_jid and turn.scheduled_thread_slot == slot
        for turn in turns
    )


def thread_is_reserved(
    child_jid: str,
    task: ScheduledTask,
    turns: list[InFlightTurn],
) -> bool:
    if any(turn.chat_jid == child_jid for turn in turns):
        return True
    session = get_session(GroupFolder(dynamic_thread_folder(task.group_folder, child_jid)))
    return session is not None and session.is_alive


def thread_name(task: ScheduledTask, slot: int) -> str:
    return f"{task.group_folder}-{slot}"


def active_parent_participant_ids(
    task: ScheduledTask,
    turns: list[InFlightTurn],
) -> tuple[str, ...]:
    """Return identifiers of humans whose active turn reserved the parent chat."""
    participant_ids: set[str] = set()
    for turn in turns:
        if turn.chat_jid != task.chat_jid or turn.work_kind is not InFlightWorkKind.INTERACTIVE:
            continue
        for message in turn.input_messages:
            sender = message.get("sender")
            if isinstance(sender, str) and sender:
                participant_ids.add(sender)
    return tuple(sorted(participant_ids))
