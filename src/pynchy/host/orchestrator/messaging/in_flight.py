"""Durable lifecycle and semantic recovery for interrupted message turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable  # noqa: TC003, RUF100 - Protocol annotations.
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pynchy.event_bus import AgentActivityEvent, Event
from pynchy.host.container_manager import OnOutput  # noqa: TC001 - beartype resolves Protocols.
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.host.orchestrator.messaging.outcomes import (  # noqa: TC001, RUF100 - beartype resolves this result annotation.
    ProcessGroupResult,
)
from pynchy.logger import logger
from pynchy.state import (
    begin_in_flight_turn,
    complete_in_flight_turn,
    get_session,
    mark_in_flight_output_sent,
    release_in_flight_turn_claim,
)
from pynchy.types import (
    ContainerOutput,
    GroupFolder,
    InFlightTurn,
    InFlightWorkKind,
    WorkspaceProfile,
)


@runtime_checkable
class InFlightMessageDeps(Protocol):
    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    async def save_state(self) -> None: ...

    async def run_agent(  # noqa: PLR0913, RUF100 - mirrors the orchestrator contract.
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        input_source: str = "user",
        turn_id: str | None = None,
    ) -> str: ...

    async def handle_streamed_output(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool: ...

    async def set_typing_on_channels(self, chat_jid: str, *, is_typing: bool) -> None: ...

    def emit(self, event: Event) -> None: ...


@dataclass(frozen=True)
class MessageTurnStart:
    turn_id: str
    chat_jid: str
    group: WorkspaceProfile
    work_kind: InFlightWorkKind
    input_messages: list[dict[str, Any]]
    input_start_cursor: str
    input_end_cursor: str
    task_id: str | None = None
    scheduled_base_chat_jid: str | None = None
    scheduled_thread_slot: int | None = None
    conversation_claim_id: str | None = None
    input_source: str = "user"


async def begin_message_turn(request: MessageTurnStart) -> InFlightTurn:
    """Checkpoint a message turn before control enters the agent runtime."""
    turn_id = request.turn_id
    chat_jid = request.chat_jid
    group = request.group
    work_kind = request.work_kind
    input_messages = request.input_messages
    input_start_cursor = request.input_start_cursor
    input_end_cursor = request.input_end_cursor
    task_id = request.task_id
    scheduled_base_chat_jid = request.scheduled_base_chat_jid
    scheduled_thread_slot = request.scheduled_thread_slot
    started_at = datetime.now(UTC).isoformat()
    session_id = await get_session(GroupFolder(group.folder))
    turn = InFlightTurn(
        turn_id=turn_id,
        chat_jid=chat_jid,
        group_folder=group.folder,
        work_kind=work_kind,
        input_messages=input_messages,
        input_start_cursor=input_start_cursor,
        input_end_cursor=input_end_cursor,
        started_at=started_at,
        task_id=task_id,
        session_id=str(session_id) if session_id else None,
        claimed_at=started_at,
        scheduled_base_chat_jid=scheduled_base_chat_jid,
        scheduled_thread_slot=scheduled_thread_slot,
        conversation_claim_id=request.conversation_claim_id,
        input_source=request.input_source,
    )
    await begin_in_flight_turn(turn)
    return turn


def _original_input_text(turn: InFlightTurn) -> str:
    parts: list[str] = []
    for message in turn.input_messages:
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        sender_name = message.get("sender_name")
        label = sender_name if isinstance(sender_name, str) and sender_name else "User"
        parts.append(f"{label}: {content}")
    return "\n\n".join(parts) or "(The original input had no text content.)"


def interrupted_resume_message(turn: InFlightTurn) -> dict[str, Any]:
    """Build an actionable continuation message without replaying the user role input."""
    prior_output = (
        "Some output may already have been shown to the user. Do not repeat it."
        if turn.output_sent
        else "No user-visible output was recorded before interruption."
    )
    content = (
        "[Deployment recovery]\n"
        "Pynchy restarted while you were working on the request below. Rehydrate the existing "
        "conversation and continue the unfinished job from its last durable state. Inspect the "
        "prior transcript, tool results, and current workspace before acting. Do not restart "
        "completed steps or repeat side effects. Finish the original request and report the final "
        f"result. {prior_output}\n\nOriginal request for reference:\n{_original_input_text(turn)}"
    )
    return {
        "message_type": "user",
        "sender": "system",
        "sender_name": "System",
        "content": content,
        "timestamp": datetime.now(UTC).isoformat(),
        "metadata": {
            "source": "deploy_continuation",
            "interrupted_turn_id": turn.turn_id,
            "deploy_id": turn.deploy_id,
        },
    }


async def note_output_sent(turn_id: str, *, already_recorded: bool) -> None:
    """Persist the first user-visible output marker without writing for every stream chunk."""
    if not already_recorded:
        await mark_in_flight_output_sent(turn_id)


async def _run_resumed_agent(
    deps: InFlightMessageDeps,
    group: WorkspaceProfile,
    turn: InFlightTurn,
    on_output: OnOutput,
) -> str:
    await deps.set_typing_on_channels(turn.chat_jid, is_typing=True)
    deps.emit(AgentActivityEvent(chat_jid=turn.chat_jid, active=True))
    try:
        return await deps.run_agent(
            group,
            turn.chat_jid,
            [interrupted_resume_message(turn)],
            on_output,
            input_source=turn.input_source,
            turn_id=turn.turn_id,
        )
    finally:
        await deps.set_typing_on_channels(turn.chat_jid, is_typing=False)
        deps.emit(AgentActivityEvent(chat_jid=turn.chat_jid, active=False))


async def resume_interrupted_message_turn(
    deps: InFlightMessageDeps,
    group: WorkspaceProfile,
    turn: InFlightTurn,
    process_pending: Callable[[str], Awaitable[ProcessGroupResult]],
) -> ProcessGroupResult:
    """Resume one claimed interactive/reset turn and then drain newer user input."""
    logger.info(
        "Resuming interrupted agent turn",
        chat_jid=turn.chat_jid,
        group=group.folder,
        turn_id=turn.turn_id,
        work_kind=turn.work_kind.value,
        deploy_id=turn.deploy_id,
    )
    output_sent = turn.output_sent
    had_error = False

    async def on_output(result: ContainerOutput) -> None:
        nonlocal output_sent, had_error
        sent = await deps.handle_streamed_output(
            turn.chat_jid,
            group,
            result,
            turn_id=turn.turn_id,
        )
        if sent and not output_sent:
            output_sent = True
            await mark_in_flight_output_sent(turn.turn_id)
        if result.status == "error":
            had_error = True

    try:
        agent_result = await _run_resumed_agent(deps, group, turn, on_output)
    except asyncio.CancelledError:
        raise
    except BaseException:
        await release_in_flight_turn_claim(turn.turn_id)
        raise

    if agent_result == "error" or had_error:
        await release_in_flight_turn_claim(turn.turn_id)
        logger.warning(
            "Interrupted agent turn requested retry",
            chat_jid=turn.chat_jid,
            turn_id=turn.turn_id,
        )
        return False

    if turn.input_end_cursor:
        await complete_turn_with_cursor(
            deps,
            turn.chat_jid,
            turn.input_end_cursor,
            turn.turn_id,
            conversation_claim_id=turn.conversation_claim_id,
        )
    else:
        await complete_in_flight_turn(
            turn.turn_id,
            conversation_claim_id=turn.conversation_claim_id,
        )

    logger.info(
        "Interrupted agent turn completed",
        chat_jid=turn.chat_jid,
        turn_id=turn.turn_id,
    )
    return await process_pending(turn.chat_jid)
