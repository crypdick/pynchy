"""Interactive work has one durable execution and retry owner."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.temporal.interactive import interactive_message_workflow_id
from pynchy.host.orchestrator.temporal.scheduler import (
    TemporalSchedulerRuntime,
    bind_scheduler_deps,
    run_interactive_message_turn,
    scheduler_workflow_runner,
)
from pynchy.host.orchestrator.temporal.workflows import InteractiveMessageWorkflow
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile
from tests.conftest import make_container_runtime_operations
from tests.temporal_scheduler_support import NullSchedulerDeps, _scheduler_runtime


@pytest.mark.parametrize(
    ("outcomes", "maximum_attempts", "expected_result", "expected_attempts"),
    [
        ([TurnOutcome.RETRY, TurnOutcome.COMPLETED], 1, None, [1]),
        ([TurnOutcome.RETRY] * 3, 3, None, [1, 2, 3]),
        ([TurnOutcome.RETRY, TurnOutcome.COMPLETED], 3, "completed", [1, 2]),
        ([ValueError("processor failed"), TurnOutcome.COMPLETED], 3, "completed", [1, 2]),
        ([TurnOutcome.PAUSED], 3, "paused", [1]),
        ([TurnOutcome.RESET], 3, "reset", [1]),
        ([TurnOutcome.COMPLETED], 3, "completed", [1]),
        (
            [TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT, TurnOutcome.COMPLETED],
            3,
            "completed",
            [1, 1],
        ),
    ],
)
async def test_workflow_owns_retry_budget_and_terminal_outcomes(
    outcomes, maximum_attempts, expected_result, expected_attempts
):
    queue = GroupQueue(
        1,
        make_container_runtime_operations(),
    )
    profile = WorkspaceProfile(jid="test:retry", name="Retry", folder="retry", trigger="always")
    attempts = []

    results = iter(outcomes)

    def process(chat_jid: str):
        assert chat_jid == profile.jid
        attempts.append(activity.info().attempt)
        result = next(results, TurnOutcome.COMPLETED)
        if isinstance(result, Exception):
            raise result
        return asyncio.sleep(0, result=result)

    queue.set_process_messages_fn(process)
    bind_scheduler_deps(NullSchedulerDeps(queue=queue, groups={profile.jid: profile}))
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(
            env.client,
            task_queue="interactive-ownership",
            workflows=[InteractiveMessageWorkflow],
            activities=[run_interactive_message_turn],
            workflow_runner=scheduler_workflow_runner(),
        ):
            result = env.client.execute_workflow(
                InteractiveMessageWorkflow.run,
                args=[profile.jid, maximum_attempts, 1.0],
                id=f"interactive-ownership-{uuid4()}",
                task_queue="interactive-ownership",
            )
            if expected_result is None:
                with pytest.raises(WorkflowFailureError):
                    await result
            else:
                assert await result == expected_result
            assert attempts == expected_attempts
    finally:
        bind_scheduler_deps(None)
        await queue.shutdown()
        await env.shutdown()


async def test_notifications_during_a_turn_coalesce_into_a_durable_follow_up():
    queue = GroupQueue(1, make_container_runtime_operations())
    profile = WorkspaceProfile(
        jid="test:follow-up", name="Follow-up", folder="follow-up", trigger="always"
    )
    started = asyncio.Event()
    finish = asyncio.Event()
    attempts = []

    async def process(chat_jid: str) -> TurnOutcome:
        attempts.append(chat_jid)
        if len(attempts) == 1:
            started.set()
            await finish.wait()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process)
    deps = NullSchedulerDeps(queue=queue, groups={profile.jid: profile})
    bind_scheduler_deps(deps)
    env = await WorkflowEnvironment.start_time_skipping()
    runtime = TemporalSchedulerRuntime(
        deps, replace(_scheduler_runtime(), temporal_task_queue="interactive-follow-up")
    )
    runtime.client = env.client
    try:
        async with Worker(
            env.client,
            task_queue="interactive-follow-up",
            workflows=[InteractiveMessageWorkflow],
            activities=[run_interactive_message_turn],
            workflow_runner=scheduler_workflow_runner(),
        ):
            await runtime.start_interactive_message_turn(profile.jid)
            await asyncio.wait_for(started.wait(), 5)
            await runtime.start_interactive_message_turn(profile.jid)
            await runtime.start_interactive_message_turn(profile.jid)
            finish.set()
            handle = env.client.get_workflow_handle(interactive_message_workflow_id(profile.jid))
            assert await handle.result() == "completed"
            assert attempts == [profile.jid, profile.jid]
            await Replayer(
                workflows=[InteractiveMessageWorkflow],
                workflow_runner=scheduler_workflow_runner(),
            ).replay_workflow(await handle.fetch_history())
            await runtime.start_interactive_message_turn(profile.jid)
            next_handle = env.client.get_workflow_handle(
                interactive_message_workflow_id(profile.jid)
            )
            assert await next_handle.result() == "completed"
            assert attempts == [profile.jid, profile.jid, profile.jid]
    finally:
        bind_scheduler_deps(None)
        await queue.shutdown()
        await env.shutdown()
