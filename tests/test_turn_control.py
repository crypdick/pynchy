"""Behavioral tests for interactive turn preparation and control outcomes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn, InFlightWorkKind
from pynchy.host.orchestrator.messaging.deps import MessageHandlerDeps
from pynchy.host.orchestrator.messaging.turn_control import (
    AgentBatch,
    TurnPreparationCallbacks,
    prepare_agent_batch,
    run_interactive_agent,
)
from pynchy.plugins.api import NewMessage
from pynchy.state import init_test_database, is_chat_paused, pause_chat
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path


def _group() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:project",
        name="Project",
        folder="project",
        trigger="@Pynchy",
    )


def _turn(control_state: CheckpointControlState) -> InFlightTurn:
    return InFlightTurn(
        turn_id="turn-1",
        chat_jid="discord:channel:project",
        group_folder="project",
        work_kind=InFlightWorkKind.INTERACTIVE,
        input_messages=[],
        input_start_cursor="",
        input_end_cursor="",
        started_at="2026-07-29T00:00:00+00:00",
        control_state=control_state,
    )


def _message(*, metadata: dict[str, bool] | None = None) -> NewMessage:
    return NewMessage(
        id="message-1",
        chat_jid="discord:channel:project",
        sender="user-1",
        sender_name="User",
        content="continue",
        timestamp="2026-07-29T00:01:00+00:00",
        metadata=metadata,
    )


def _deps() -> MagicMock:
    deps = MagicMock(spec=MessageHandlerDeps)
    deps.last_agent_timestamp = {}
    deps.repo_is_dirty.return_value = False
    deps.set_typing_on_channels = AsyncMock()
    deps.emit = MagicMock()
    deps.new_learning_run_summary.return_value = object()
    deps.observe_learning_output = MagicMock()
    deps.handle_streamed_output = AsyncMock(return_value=False)
    return deps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_state", "expected"),
    [
        (CheckpointControlState.PAUSE_REQUESTED, TurnOutcome.RETRY),
        (CheckpointControlState.RESET_REQUESTED, TurnOutcome.RESET),
        (CheckpointControlState.PAUSED, TurnOutcome.COMPLETED),
    ],
)
async def test_prepare_agent_batch_honors_checkpoint_control_state(
    control_state, expected, tmp_path: Path
) -> None:
    await init_test_database()
    deps = _deps()
    callbacks = TurnPreparationCallbacks(
        process_pending=AsyncMock(),
        get_pending_messages=AsyncMock(return_value=[_message()]),
    )
    checkpoint = _turn(control_state)

    with (
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.resume_interrupted_message_if_present",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.handle_reset_handoff",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.get_oldest_resumable_turn_for_group",
            new_callable=AsyncMock,
            return_value=checkpoint,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.should_skip_batch",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.prepare_message_context",
            return_value=([{"content": "continue"}], []),
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.clear_in_flight_turn",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.resume_paused_in_flight_turn",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await prepare_agent_batch(
            deps,
            "discord:channel:project",
            _group(),
            tmp_path,
            callbacks,
        )

    assert result is expected


@pytest.mark.asyncio
async def test_prepare_agent_batch_returns_paused_when_only_paused_work_remains(
    tmp_path: Path,
) -> None:
    deps = _deps()
    callbacks = TurnPreparationCallbacks(
        process_pending=AsyncMock(),
        get_pending_messages=AsyncMock(return_value=[]),
    )

    with (
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.resume_interrupted_message_if_present",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.handle_reset_handoff",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.get_oldest_resumable_turn_for_group",
            new_callable=AsyncMock,
            return_value=_turn(CheckpointControlState.PAUSED),
        ),
    ):
        result = await prepare_agent_batch(
            deps,
            "discord:channel:project",
            _group(),
            tmp_path,
            callbacks,
        )

    assert result is TurnOutcome.PAUSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected_paused"),
    [
        (None, False),
        ({"synthetic_user_input": True}, True),
    ],
)
async def test_prepare_agent_batch_returns_fresh_batch_for_active_checkpoint(
    tmp_path: Path,
    metadata: dict[str, bool] | None,
    expected_paused: bool,
) -> None:
    await init_test_database()
    deps = _deps()
    await pause_chat("discord:channel:project")
    callbacks = TurnPreparationCallbacks(
        process_pending=AsyncMock(),
        get_pending_messages=AsyncMock(return_value=[_message(metadata=metadata)]),
    )

    with (
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.resume_interrupted_message_if_present",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.handle_reset_handoff",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.get_oldest_resumable_turn_for_group",
            new_callable=AsyncMock,
            return_value=_turn(CheckpointControlState.ACTIVE),
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.should_skip_batch",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.turn_control.prepare_message_context",
            return_value=([{"content": "continue"}], []),
        ),
    ):
        result = await prepare_agent_batch(
            deps,
            "discord:channel:project",
            _group(),
            tmp_path,
            callbacks,
        )

    assert isinstance(result, AgentBatch)
    assert result.messages == [{"content": "continue"}]
    assert await is_chat_paused("discord:channel:project") is expected_paused


@pytest.mark.asyncio
async def test_interactive_agent_treats_cancelled_agent_with_control_as_handled() -> None:
    deps = _deps()
    deps.run_agent = AsyncMock(side_effect=asyncio.CancelledError())

    with patch(
        "pynchy.host.orchestrator.messaging.turn_control.requested_control_outcome",
        new_callable=AsyncMock,
        return_value=TurnOutcome.PAUSED,
    ):
        result = await run_interactive_agent(
            deps, _group(), [], [], _turn(CheckpointControlState.ACTIVE)
        )

    assert result.agent_result == "error"
    assert result.control_outcome is TurnOutcome.PAUSED


@pytest.mark.asyncio
async def test_interactive_agent_treats_failed_agent_with_control_as_handled() -> None:
    deps = _deps()
    deps.run_agent = AsyncMock(side_effect=RuntimeError("agent stopped"))

    with patch(
        "pynchy.host.orchestrator.messaging.turn_control.requested_control_outcome",
        new_callable=AsyncMock,
        return_value=TurnOutcome.RESET,
    ):
        result = await run_interactive_agent(
            deps, _group(), [], [], _turn(CheckpointControlState.ACTIVE)
        )

    assert result.agent_result == "error"
    assert result.control_outcome is TurnOutcome.RESET
