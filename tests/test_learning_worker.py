"""Tests for the background Obsidian learning worker."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.learning.paths import LearningPaths
from pynchy.host.learning.queue import (
    ClaimedLearningPacket,
    LearningPacket,
    LearningQueue,
)
from pynchy.host.learning.worker import (
    LearningWorkerDeps,
    process_one_learning_job,
    start_learning_worker_loop,
)
from pynchy.types import WorkspaceProfile


def _packet(
    *,
    messages: list[dict[str, str]] | None = None,
    profile: str = "Deep Work",
    tool_counts: dict[str, int] | None = None,
    error_snippets: list[str] | None = None,
) -> LearningPacket:
    return LearningPacket(
        job_id="learning-1",
        chat_jid="slack:C123",
        group_folder="research",
        profile=profile,
        created_at="2026-07-07T10:00:00+00:00",
        messages=messages or [{"role": "user", "content": "remember this workflow"}],
        final_answer="Done.",
        tool_counts=tool_counts or {},
        error_snippets=error_snippets or [],
        loaded_skills=[],
        provenance={"run_id": "run-123"},
    )


def _claimed(packet: LearningPacket | None = None) -> ClaimedLearningPacket:
    return ClaimedLearningPacket(
        packet=packet or _packet(),
        path=Path("/queue/claimed/learning-1.json"),
        claim_id="claim-1",
    )


def _paths(tmp_path: Path) -> LearningPaths:
    vault_root = tmp_path / "vault"
    profile_root = vault_root / "systems/pynchy/profiles/deep-work"
    return LearningPaths(
        profile="Deep Work",
        profile_slug="deep-work",
        vault_root=vault_root,
        vault_mount_path="/workspace/vault",
        profile_root=profile_root,
        memory_root=profile_root / "memory",
        skills_root=profile_root / "skills",
        mounted_profile_root="/workspace/vault/systems/pynchy/profiles/deep-work",
        mounted_memory_root="/workspace/vault/systems/pynchy/profiles/deep-work/memory",
        mounted_skills_root="/workspace/vault/systems/pynchy/profiles/deep-work/skills",
    )


class _FakeQueue(LearningQueue):
    def __init__(self, claimed: ClaimedLearningPacket | None) -> None:
        self._claimed = claimed
        self.calls: list[str] = []
        self.completed: list[ClaimedLearningPacket] = []
        self.failed: list[tuple[ClaimedLearningPacket, str]] = []

    def requeue_expired(self) -> int:
        self.calls.append("requeue")
        return 0

    def claim_next(self) -> ClaimedLearningPacket | None:
        self.calls.append("claim")
        return self._claimed

    def complete(self, claimed: ClaimedLearningPacket) -> Path:
        self.calls.append("complete")
        self.completed.append(claimed)
        return claimed.path.with_suffix(".done")

    def fail(self, claimed: ClaimedLearningPacket, reason: str) -> Path:
        self.calls.append("fail")
        self.failed.append((claimed, reason))
        return claimed.path.with_suffix(".error")


@dataclass(frozen=True)
class _RunAgentCall:
    group: WorkspaceProfile
    chat_jid: str
    messages: list[dict[str, Any]]
    on_output: Callable[[Any], Any] | None
    extra_system_notices: list[str] | None
    is_scheduled_task: bool
    repo_access_override: str | None
    input_source: str


class _FakeRunner:
    def __init__(
        self,
        *,
        result: str = "success",
        error: BaseException | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[_RunAgentCall] = []
        self.output_callback_was_called = False

    async def __call__(
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: Callable[[Any], Any] | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
    ) -> str:
        self.calls.append(
            _RunAgentCall(
                group=group,
                chat_jid=chat_jid,
                messages=messages,
                on_output=on_output,
                extra_system_notices=extra_system_notices,
                is_scheduled_task=is_scheduled_task,
                repo_access_override=repo_access_override,
                input_source=input_source,
            )
        )
        if self._error is not None:
            raise self._error
        if on_output is not None:
            callback_result = on_output({"type": "reviewer-output", "content": "captured"})
            if inspect.isawaitable(callback_result):
                await callback_result
            self.output_callback_was_called = True
        return self._result


def _deps(queue: _FakeQueue, runner: _FakeRunner) -> LearningWorkerDeps:
    return LearningWorkerDeps(
        run_agent=cast(Callable[..., Awaitable[str]], runner),
        queue=cast(LearningQueue, queue),
    )


@pytest.mark.asyncio
async def test_process_one_requeues_expired_before_claiming_and_returns_false_when_empty() -> None:
    queue = _FakeQueue(claimed=None)
    runner = _FakeRunner()

    result = await process_one_learning_job(_deps(queue, runner))

    assert result is False
    assert queue.calls == ["requeue", "claim"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_process_one_completes_skipped_packet_without_running_agent() -> None:
    claimed = _claimed(
        _packet(
            messages=[{"role": "user", "content": "thanks!"}],
            tool_counts={},
            error_snippets=[],
        )
    )
    queue = _FakeQueue(claimed=claimed)
    runner = _FakeRunner()

    result = await process_one_learning_job(_deps(queue, runner))

    assert result is True
    assert queue.calls == ["requeue", "claim", "complete"]
    assert queue.completed == [claimed]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_process_one_runs_hidden_reviewer_and_completes_success(
    tmp_path: Path,
) -> None:
    claimed = _claimed()
    queue = _FakeQueue(claimed=claimed)
    runner = _FakeRunner(result="success")

    with patch("pynchy.host.learning.worker.resolve_learning_paths", return_value=_paths(tmp_path)):
        result = await process_one_learning_job(_deps(queue, runner))

    assert result is True
    assert queue.calls == ["requeue", "claim", "complete"]
    assert queue.completed == [claimed]
    assert runner.output_callback_was_called is True
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.group == WorkspaceProfile(
        jid="learning-review:deep-work",
        name="Learning Reviewer",
        folder="learning-review-deep-work",
        trigger="",
        is_admin=False,
    )
    assert call.chat_jid == "learning-review:deep-work"
    assert call.chat_jid != claimed.packet.chat_jid
    assert call.is_scheduled_task is True
    assert call.input_source == "hidden_learning_review"
    assert call.repo_access_override is None
    assert call.extra_system_notices is None
    assert len(call.messages) == 1
    assert call.messages[0]["role"] == "user"
    assert "The mounted vault root is the global memory namespace." in call.messages[0]["content"]


@pytest.mark.asyncio
async def test_process_one_fails_when_learning_paths_are_unavailable() -> None:
    claimed = _claimed()
    queue = _FakeQueue(claimed=claimed)
    runner = _FakeRunner()

    with patch("pynchy.host.learning.worker.resolve_learning_paths", return_value=None):
        result = await process_one_learning_job(_deps(queue, runner))

    assert result is True
    assert queue.calls == ["requeue", "claim", "fail"]
    assert runner.calls == []
    assert queue.failed[0][0] == claimed
    assert "learning paths" in queue.failed[0][1]


@pytest.mark.asyncio
async def test_process_one_fails_retryably_when_reviewer_returns_non_success(
    tmp_path: Path,
) -> None:
    claimed = _claimed()
    queue = _FakeQueue(claimed=claimed)
    runner = _FakeRunner(result="error")

    with patch("pynchy.host.learning.worker.resolve_learning_paths", return_value=_paths(tmp_path)):
        result = await process_one_learning_job(_deps(queue, runner))

    assert result is True
    assert queue.calls == ["requeue", "claim", "fail"]
    assert queue.failed[0][0] == claimed
    assert "error" in queue.failed[0][1]


@pytest.mark.asyncio
async def test_process_one_fails_retryably_when_reviewer_raises(tmp_path: Path) -> None:
    claimed = _claimed()
    queue = _FakeQueue(claimed=claimed)
    runner = _FakeRunner(error=RuntimeError("container exploded"))

    with patch("pynchy.host.learning.worker.resolve_learning_paths", return_value=_paths(tmp_path)):
        result = await process_one_learning_job(_deps(queue, runner))

    assert result is True
    assert queue.calls == ["requeue", "claim", "fail"]
    assert "container exploded" in queue.failed[0][1]


@pytest.mark.asyncio
async def test_process_one_propagates_cancelled_error(tmp_path: Path) -> None:
    claimed = _claimed()
    queue = _FakeQueue(claimed=claimed)
    runner = _FakeRunner(error=asyncio.CancelledError())

    with (
        patch("pynchy.host.learning.worker.resolve_learning_paths", return_value=_paths(tmp_path)),
        pytest.raises(asyncio.CancelledError),
    ):
        await process_one_learning_job(_deps(queue, runner))

    assert queue.calls == ["requeue", "claim"]
    assert queue.failed == []


@pytest.mark.asyncio
async def test_worker_loop_sleeps_when_no_job_and_propagates_cancellation() -> None:
    deps = _deps(_FakeQueue(claimed=None), _FakeRunner())
    settings = make_settings(
        learning=LearningConfig(queue_poll_interval_seconds=0.25),
    )
    process_mock = AsyncMock(return_value=False)
    sleep_mock = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch("pynchy.host.learning.worker.process_one_learning_job", process_mock),
        patch("pynchy.host.learning.worker.asyncio.sleep", sleep_mock),
        patch("pynchy.host.learning.worker.get_settings", return_value=settings),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_learning_worker_loop(deps)

    process_mock.assert_awaited_once_with(deps)
    sleep_mock.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_worker_loop_continues_after_non_cancellation_error() -> None:
    deps = _deps(_FakeQueue(claimed=None), _FakeRunner())
    settings = make_settings(
        learning=LearningConfig(queue_poll_interval_seconds=0.25),
    )
    process_mock = AsyncMock(side_effect=[RuntimeError("queue exploded"), False])
    sleep_calls: list[float] = []

    async def sleep_until_second_loop(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) == 2:
            raise asyncio.CancelledError

    with (
        patch("pynchy.host.learning.worker.process_one_learning_job", process_mock),
        patch("pynchy.host.learning.worker.asyncio.sleep", sleep_until_second_loop),
        patch("pynchy.host.learning.worker.get_settings", return_value=settings),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_learning_worker_loop(deps)

    assert process_mock.await_count == 2
    assert sleep_calls == [0.25, 0.25]


@pytest.mark.asyncio
async def test_worker_loop_propagates_process_cancellation() -> None:
    deps = _deps(_FakeQueue(claimed=None), _FakeRunner())
    process_mock = AsyncMock(side_effect=asyncio.CancelledError)
    sleep_mock = AsyncMock()

    with (
        patch("pynchy.host.learning.worker.process_one_learning_job", process_mock),
        patch("pynchy.host.learning.worker.asyncio.sleep", sleep_mock),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_learning_worker_loop(deps)

    process_mock.assert_awaited_once_with(deps)
    sleep_mock.assert_not_awaited()
