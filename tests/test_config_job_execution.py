"""Current workspace-owned configuration-job execution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.orchestrator.config_job_execution import (
    prepare_config_job,
    run_deterministic_config_job,
)
from pynchy.host.orchestrator.host_shell import ShellResult
from pynchy.scheduling.api import ScheduledTask, SessionPolicy


@dataclass
class _Deps:
    messages: list[tuple[str, str]] = field(default_factory=list)

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        self.messages.append((chat_jid, text))


def _task(**changes: object) -> ScheduledTask:
    task = ScheduledTask(
        id="job-1",
        group_folder="ops",
        chat_jid="slack:ops",
        prompt="Inspect the report",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        session_policy=SessionPolicy.CONTINUE,
        config_job_name="report",
        config_job_is_deterministic=True,
        config_job_command="scripts/report.py",
        config_job_cwd="/workspace/ops",
        bound_chat_jid="slack:ops:report",
    )
    return replace(task, **changes)


@pytest.mark.asyncio
async def test_pre_run_false_gate_skips_the_agent_before_thread_creation() -> None:
    task = _task(
        config_job_pre_run_command="scripts/should-wake.py",
        config_job_pre_run_cwd="/workspace/ops",
    )
    runner = AsyncMock(
        return_value=ShellResult(returncode=0, stdout='{"wakeAgent": false}', stderr="")
    )

    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        runner,
    ):
        prepared, outcome = await prepare_config_job(task, Path("/memory/ops"))

    assert prepared is None
    assert outcome == "Skipped: wakeAgent=false"
    assert runner.await_args.kwargs["env"] == {
        "PYNCHY_SCHEDULED_JOB": "1",
        "PYNCHY_AUTOMATION_MEMORY_DIR": "/memory/ops",
    }


@pytest.mark.asyncio
async def test_pre_run_failure_is_attached_to_the_agent_prompt() -> None:
    task = _task(
        config_job_pre_run_command="scripts/inspect.py",
        config_job_pre_run_cwd="/workspace/ops",
    )
    result = ShellResult(
        returncode=1,
        stdout="report output",
        stderr="report error",
        timed_out=True,
        start_error="bridge unavailable",
    )

    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=result),
    ):
        prepared, outcome = await prepare_config_job(task)

    assert outcome is None
    assert prepared is not None
    assert prepared.prompt.endswith(
        "exit_code: 1\ntimed_out: true\nstart_error: bridge unavailable"
        "\n\nstdout:\nreport output\n\nstderr:\nreport error"
    )


@pytest.mark.asyncio
async def test_pre_run_output_is_bounded() -> None:
    task = _task(
        config_job_pre_run_command="scripts/inspect.py",
        config_job_pre_run_cwd="/workspace/ops",
    )
    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=ShellResult(returncode=0, stdout="x" * 13_000, stderr="")),
    ):
        prepared, outcome = await prepare_config_job(task)

    assert outcome is None
    assert prepared is not None
    assert len(prepared.prompt) < len(task.prompt) + 13_000


@pytest.mark.asyncio
async def test_pre_run_prompt_omits_empty_stdout() -> None:
    task = _task(
        config_job_pre_run_command="scripts/inspect.py",
        config_job_pre_run_cwd="/workspace/ops",
    )
    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=ShellResult(returncode=1, stdout="", stderr="")),
    ):
        prepared, outcome = await prepare_config_job(task)

    assert outcome is None
    assert prepared is not None
    assert prepared.prompt.count("\n") == task.prompt.count("\n") + 4


@pytest.mark.asyncio
async def test_pre_run_requires_a_working_directory() -> None:
    task = _task(config_job_pre_run_command="scripts/inspect.py", config_job_pre_run_cwd=None)

    with pytest.raises(RuntimeError, match="pre-run execution is incomplete"):
        await prepare_config_job(task)


@pytest.mark.asyncio
async def test_deterministic_job_broadcasts_output_and_reports_timeout() -> None:
    deps = _Deps()
    result = ShellResult(
        returncode=124, stdout="partial report", stderr="deadline exceeded", timed_out=True
    )

    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=result),
    ):
        execution = await run_deterministic_config_job(_task(), deps)

    assert execution is not None
    assert execution.error == "Workspace job report timed out"
    assert execution.result.startswith("partial report")
    assert deps.messages == [
        (
            "slack:ops:report",
            "partial report\n\n[Pynchy workspace job: report] exit_code=124\ndeadline exceeded",
        )
    ]


@pytest.mark.asyncio
async def test_deterministic_job_requires_a_durable_delivery_binding() -> None:
    deps = _Deps()
    task = _task(bound_chat_jid=None)

    with (
        patch(
            "pynchy.host.orchestrator.config_job_execution.run_shell_command",
            AsyncMock(return_value=ShellResult(returncode=0, stdout="finished", stderr="")),
        ),
        pytest.raises(RuntimeError, match="no durable destination binding"),
    ):
        await run_deterministic_config_job(task, deps)

    assert deps.messages == []


@pytest.mark.asyncio
async def test_deterministic_job_without_output_completes_without_delivery() -> None:
    deps = _Deps()

    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=ShellResult(returncode=0, stdout="", stderr="")),
    ):
        execution = await run_deterministic_config_job(_task(bound_chat_jid=None), deps)

    assert execution is not None
    assert execution.result == "Completed"
    assert execution.error is None
    assert deps.messages == []


@pytest.mark.asyncio
async def test_only_complete_deterministic_jobs_reach_the_shell() -> None:
    deps = _Deps()

    assert await run_deterministic_config_job(_task(config_job_name=None), deps) is None
    assert (
        await run_deterministic_config_job(_task(config_job_is_deterministic=False), deps) is None
    )
    with pytest.raises(RuntimeError, match="execution is incomplete"):
        await run_deterministic_config_job(_task(config_job_command=None), deps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "error"),
    [
        (ShellResult(returncode=1, stdout="", stderr=""), "exited with code 1"),
        (
            ShellResult(returncode=0, stdout="", stderr="", start_error="spawn failed"),
            "failed to start",
        ),
    ],
)
async def test_deterministic_job_reports_shell_failures(result: ShellResult, error: str) -> None:
    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=result),
    ):
        execution = await run_deterministic_config_job(_task(), _Deps())

    assert execution is not None
    assert error in (execution.error or "")


@pytest.mark.asyncio
async def test_deterministic_job_false_gate_skips_execution() -> None:
    with patch(
        "pynchy.host.orchestrator.config_job_execution.run_shell_command",
        AsyncMock(return_value=ShellResult(returncode=0, stdout='{"wakeAgent": false}', stderr="")),
    ):
        execution = await run_deterministic_config_job(_task(), _Deps())

    assert execution is not None
    assert execution.result == "Skipped: wakeAgent=false"
    assert execution.error is None
