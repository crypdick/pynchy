"""Additional public behavior tests for scheduled task execution."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn, InFlightWorkKind
from pynchy.host.orchestrator import task_scheduler as scheduler
from pynchy.host.orchestrator.scheduled_turn import TaskAgentResult
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent, start_scheduler_loop
from pynchy.state import init_test_database
from pynchy.turn_outcomes import TurnOutcome
from tests.task_scheduler_support import (
    RecordingTemporalRuntime,
    _patch_settings,
)

pytest_plugins = ("tests.task_scheduler_support",)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


class TestSchedulerExecutionEdges:
    @pytest.mark.asyncio
    async def test_config_job_awaiting_reconciliation_retries(
        self, mock_deps, sample_task, tmp_path
    ):
        sample_task.config_job_name = "daily-check"
        sample_task.config_job_is_deterministic = None
        logged = AsyncMock()

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", logged),
            _patch_settings(groups_dir=tmp_path),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.RETRY

        logged.assert_awaited_once()
        assert logged.await_args.args[0].error == (
            "Config job execution is awaiting reconciliation"
        )

    @pytest.mark.asyncio
    async def test_reset_requested_turn_is_cleared_before_execution(self, mock_deps, sample_task):
        turn = InFlightTurn(
            turn_id="reset-turn",
            chat_jid=sample_task.chat_jid,
            group_folder=sample_task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-30T00:00:00+00:00",
            task_id=sample_task.id,
            control_state=CheckpointControlState.RESET_REQUESTED,
        )
        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_in_flight_turn_for_task",
                AsyncMock(return_value=turn),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.clear_in_flight_turn",
                AsyncMock(),
            ) as clear,
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.RESET

        clear.assert_awaited_once_with(turn.turn_id)

    @pytest.mark.asyncio
    async def test_missing_binding_is_logged_and_retried(self, mock_deps, sample_task, tmp_path):
        task = replace(sample_task, bound_chat_jid=None, bound_group_folder=None)
        logged = AsyncMock()

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", logged),
            _patch_settings(groups_dir=tmp_path),
        ):
            assert await run_scheduled_agent(task, mock_deps) is TurnOutcome.RETRY

        assert "no durable conversation binding" in logged.await_args.args[0].error

    @pytest.mark.asyncio
    async def test_already_claimed_interrupted_turn_is_completed_without_resume(
        self, mock_deps, sample_task, sample_group
    ):
        mock_deps.groups[sample_group.jid] = sample_group
        turn = InFlightTurn(
            turn_id="claimed-turn",
            chat_jid=sample_task.chat_jid,
            group_folder=sample_task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-30T00:00:00+00:00",
            task_id=sample_task.id,
        )
        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_in_flight_turn_for_task",
                AsyncMock(return_value=turn),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.claim_in_flight_turn",
                AsyncMock(return_value=False),
            ),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

        assert mock_deps.agent_runs == []

    @pytest.mark.asyncio
    async def test_interrupted_turn_with_invalid_timestamp_can_finish_controlled_run(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        mock_deps.groups[sample_group.jid] = sample_group
        turn = InFlightTurn(
            turn_id="invalid-timestamp-turn",
            chat_jid=sample_task.chat_jid,
            group_folder=sample_task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="not-an-iso-timestamp",
            task_id=sample_task.id,
        )
        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_in_flight_turn_for_task",
                AsyncMock(return_value=turn),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.claim_in_flight_turn",
                AsyncMock(return_value=True),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.run_task_agent",
                AsyncMock(
                    return_value=TaskAgentResult(
                        turn_id=turn.turn_id,
                        result=None,
                        error=None,
                        terminal_outcome=TurnOutcome.COMPLETED,
                    )
                ),
            ),
            _patch_settings(groups_dir=tmp_path),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_terminal_agent_outcome_stops_normal_execution(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        mock_deps.groups[sample_group.jid] = sample_group
        terminal = TaskAgentResult(
            turn_id="terminal-turn",
            result=None,
            error=None,
            terminal_outcome=TurnOutcome.PAUSED,
        )
        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.apply_scheduled_session_policy",
                AsyncMock(return_value=sample_task),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.run_deterministic_config_job",
                AsyncMock(return_value=None),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.prepare_config_job",
                AsyncMock(return_value=(sample_task, None)),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.run_task_agent",
                AsyncMock(return_value=terminal),
            ),
            _patch_settings(groups_dir=tmp_path),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.PAUSED


@pytest.mark.asyncio
async def test_scheduler_builds_lazy_temporal_runtime_when_not_preconfigured(
    mock_deps,
):
    def stop_after_poll(_delay: float) -> None:
        raise asyncio.CancelledError

    with (
        patch.object(scheduler, "TemporalSchedulerRuntime", None),
        patch.object(
            scheduler,
            "get_temporal_scheduler_runtime",
            return_value=RecordingTemporalRuntime,
        ),
        patch.object(scheduler.asyncio, "sleep", side_effect=stop_after_poll),
        contextlib.suppress(asyncio.CancelledError),
    ):
        await start_scheduler_loop(mock_deps)
