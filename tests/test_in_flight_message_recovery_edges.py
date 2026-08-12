"""Public interrupted-message recovery behavior at agent and control boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
    OnOutput,
)
from pynchy.host.orchestrator.messaging import in_flight
from pynchy.host.orchestrator.messaging.in_flight import (
    MessageTurnStart,
    begin_message_turn,
    interrupted_resume_message,
    note_output_sent,
    requested_control_outcome,
    resume_interrupted_message_turn,
    semantic_resume_messages,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import RuntimeTarget, WorkspaceProfile

if TYPE_CHECKING:
    from pynchy.event_bus import Event


def _workspace() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="slack:group",
        name="Group",
        folder="group",
        trigger="!pynchy",
    )


def _turn(
    *,
    input_messages: list[dict[str, Any]] | None = None,
    output_sent: bool = False,
    control_state: CheckpointControlState = CheckpointControlState.ACTIVE,
    input_end_cursor: str = "end",
    chat_jid: str = "slack:group",
) -> InFlightTurn:
    return InFlightTurn(
        turn_id="turn-1",
        chat_jid=chat_jid,
        group_folder="group",
        work_kind=InFlightWorkKind.INTERACTIVE,
        input_messages=input_messages or [{"content": "resume this"}],
        input_start_cursor="start",
        input_end_cursor=input_end_cursor,
        started_at="2026-07-31T10:00:00+00:00",
        output_sent=output_sent,
        control_state=control_state,
    )


@dataclass
class _Deps:
    agent_result: str = "success"
    output: ContainerOutput | None = None
    raise_error: BaseException | None = None
    output_sent: bool = True
    typing: list[tuple[str, bool]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    streamed: list[ContainerOutput] = field(default_factory=list)
    last_agent_timestamp: dict[str, str] = field(default_factory=dict)

    async def save_state(self) -> None:
        return None

    async def run_agent(
        self,
        _group: WorkspaceProfile,
        _chat_jid: str,
        _messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        input_source: str = "user",
        turn_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> str:
        del extra_system_notices, input_source, turn_id, resume_session_id
        if self.raise_error is not None:
            raise self.raise_error
        if self.output is not None and on_output is not None:
            await on_output(self.output)
        return self.agent_result

    async def handle_streamed_output(
        self,
        _chat_jid: str,
        _group: WorkspaceProfile,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool:
        del turn_id
        self.streamed.append(result)
        return self.output_sent

    async def set_typing_on_channels(self, chat_jid: str, *, is_typing: bool) -> None:
        self.typing.append((chat_jid, is_typing))

    def emit(self, event: Event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_begin_message_turn_persists_existing_session_and_claim_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _workspace()
    persist = AsyncMock()
    monkeypatch.setattr(in_flight, "get_session", AsyncMock(return_value="session-1"))
    monkeypatch.setattr(in_flight, "begin_in_flight_turn", persist)

    turn = await begin_message_turn(
        MessageTurnStart(
            turn_id="turn-1",
            chat_jid=group.jid,
            group=group,
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[{"content": "hello"}],
            input_start_cursor="cursor-1",
            input_end_cursor="cursor-2",
            conversation_claim_id="claim-1",
            input_source="webhook",
        )
    )

    assert turn.session_id == "session-1"
    assert turn.conversation_claim_id == "claim-1"
    assert turn.input_source == "webhook"
    persist.assert_awaited_once_with(turn)


def test_resume_message_preserves_user_text_and_excludes_checkpoint_guidance() -> None:
    turn = _turn(
        input_messages=[
            {"content": "original", "sender_name": "Sender"},
            {"content": "   "},
            {"content": 42},
            {"content": "new guidance", "metadata": {"checkpoint_guidance": True}},
        ],
        output_sent=True,
        control_state=CheckpointControlState.PAUSED,
    )

    message = interrupted_resume_message(turn)
    messages = semantic_resume_messages(turn)

    assert message["metadata"]["source"] == "pause_continuation"
    original = turn.input_messages[0]["content"]
    guidance = turn.input_messages[-1]["content"]
    assert isinstance(original, str)
    assert isinstance(guidance, str)
    assert original in message["content"]
    assert message["content"].endswith(f"Sender: {original}")
    assert messages[1:] == [{"content": guidance, "metadata": {"checkpoint_guidance": True}}]


@pytest.mark.asyncio
async def test_requested_control_outcome_finishes_reset_and_failed_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(in_flight, "get_in_flight_turn", AsyncMock(return_value=None))
    assert await requested_control_outcome("missing", agent_succeeded=True) is None

    reset = _turn(control_state=CheckpointControlState.RESET_REQUESTED)
    clear = AsyncMock()
    monkeypatch.setattr(in_flight, "get_in_flight_turn", AsyncMock(return_value=reset))
    monkeypatch.setattr(in_flight, "clear_in_flight_turn", clear)

    assert await requested_control_outcome("turn-1", agent_succeeded=True) is TurnOutcome.RESET
    clear.assert_awaited_once_with("turn-1")

    paused = _turn(control_state=CheckpointControlState.PAUSE_REQUESTED)
    finalize = AsyncMock()
    monkeypatch.setattr(in_flight, "get_in_flight_turn", AsyncMock(return_value=paused))
    monkeypatch.setattr(in_flight, "finalize_in_flight_pause", finalize)

    assert await requested_control_outcome("turn-1", agent_succeeded=False) is TurnOutcome.PAUSED
    finalize.assert_awaited_once_with("turn-1")


@pytest.mark.asyncio
async def test_note_output_sent_only_persists_first_output(monkeypatch: pytest.MonkeyPatch) -> None:
    mark = AsyncMock()
    monkeypatch.setattr(in_flight, "mark_in_flight_output_sent", mark)

    await note_output_sent("turn-1", already_recorded=True)
    await note_output_sent("turn-1", already_recorded=False)

    mark.assert_awaited_once_with("turn-1")


@pytest.mark.asyncio
async def test_resume_message_completes_and_marks_first_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps(output=ContainerOutput(status="success", result="visible"))
    mark = AsyncMock()
    complete = AsyncMock()
    process_pending = AsyncMock(return_value=TurnOutcome.COMPLETED)
    monkeypatch.setattr(in_flight, "mark_in_flight_output_sent", mark)
    monkeypatch.setattr(in_flight, "requested_control_outcome", AsyncMock(return_value=None))
    monkeypatch.setattr(in_flight, "complete_turn_with_cursor", complete)

    outcome = await resume_interrupted_message_turn(
        deps,
        RuntimeTarget.from_binding("group", "slack:group"),
        _workspace(),
        _turn(),
        process_pending,
    )

    assert outcome is TurnOutcome.COMPLETED
    assert deps.typing == [("slack:group", True), ("slack:group", False)]
    assert len(deps.events) == 2
    mark.assert_awaited_once_with("turn-1")
    complete.assert_awaited_once()
    process_pending.assert_awaited_once_with("slack:group")


@pytest.mark.asyncio
async def test_resume_message_retries_after_streamed_error_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps(output=ContainerOutput(status="error", error="agent failed"))
    release = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(in_flight, "requested_control_outcome", AsyncMock(return_value=None))
    monkeypatch.setattr(in_flight, "release_in_flight_turn_claim", release)
    monkeypatch.setattr(in_flight, "mark_in_flight_output_sent", mark)

    outcome = await resume_interrupted_message_turn(
        deps,
        RuntimeTarget.from_binding("group", "slack:group"),
        _workspace(),
        _turn(),
        AsyncMock(),
    )

    assert outcome is TurnOutcome.RETRY
    mark.assert_awaited_once_with("turn-1")
    release.assert_awaited_once_with("turn-1")


@pytest.mark.asyncio
async def test_resume_message_honors_control_outcome_after_agent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps()
    control = AsyncMock(return_value=TurnOutcome.RESET)
    monkeypatch.setattr(in_flight, "requested_control_outcome", control)

    outcome = await resume_interrupted_message_turn(
        deps,
        RuntimeTarget.from_binding("group", "slack:group"),
        _workspace(),
        _turn(),
        AsyncMock(),
    )

    assert outcome is TurnOutcome.RESET
    control.assert_awaited_once_with("turn-1", agent_succeeded=True)


@pytest.mark.asyncio
async def test_resume_message_releases_claim_when_agent_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps(raise_error=RuntimeError("agent crashed"))
    release = AsyncMock()
    monkeypatch.setattr(in_flight, "requested_control_outcome", AsyncMock(return_value=None))
    monkeypatch.setattr(in_flight, "release_in_flight_turn_claim", release)

    with pytest.raises(RuntimeError, match="agent crashed"):
        await resume_interrupted_message_turn(
            deps,
            RuntimeTarget.from_binding("group", "slack:group"),
            _workspace(),
            _turn(),
            AsyncMock(),
        )

    release.assert_awaited_once_with("turn-1")


@pytest.mark.asyncio
async def test_resume_message_propagates_cancellation_without_claim_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps(raise_error=asyncio.CancelledError())
    release = AsyncMock()
    monkeypatch.setattr(in_flight, "requested_control_outcome", AsyncMock(return_value=None))
    monkeypatch.setattr(in_flight, "release_in_flight_turn_claim", release)

    with pytest.raises(asyncio.CancelledError):
        await resume_interrupted_message_turn(
            deps,
            RuntimeTarget.from_binding("group", "slack:group"),
            _workspace(),
            _turn(),
            AsyncMock(),
        )

    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_message_returns_control_outcome_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps(raise_error=asyncio.CancelledError())
    monkeypatch.setattr(
        in_flight,
        "requested_control_outcome",
        AsyncMock(return_value=TurnOutcome.PAUSED),
    )

    outcome = await resume_interrupted_message_turn(
        deps,
        RuntimeTarget.from_binding("group", "slack:group"),
        _workspace(),
        _turn(),
        AsyncMock(),
    )

    assert outcome is TurnOutcome.PAUSED


@pytest.mark.asyncio
async def test_resume_message_returns_control_outcome_on_agent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _Deps(raise_error=RuntimeError("agent crashed"))
    monkeypatch.setattr(
        in_flight,
        "requested_control_outcome",
        AsyncMock(return_value=TurnOutcome.RESET),
    )

    outcome = await resume_interrupted_message_turn(
        deps,
        RuntimeTarget.from_binding("group", "slack:group"),
        _workspace(),
        _turn(),
        AsyncMock(),
    )

    assert outcome is TurnOutcome.RESET
