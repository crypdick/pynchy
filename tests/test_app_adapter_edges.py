"""Public adapter edge behavior for application-level orchestration contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import pynchy.host.orchestrator.app as app_module
from pynchy.host.container_manager.security.approval import configure_approval_state_root
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.learning_packets import LearningPacket
from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def _learning_packet() -> LearningPacket:
    return LearningPacket(
        job_id="job-1",
        chat_jid="chat",
        group_folder="chat",
        profile="default",
        created_at="2026-07-31T00:00:00Z",
        messages=[],
        final_answer=None,
        tool_counts={},
        error_snippets=[],
        loaded_skills=[],
        provenance={},
    )


async def test_application_reviews_linear_plan_with_current_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    request = LinearPlanReviewRequest(
        workspace="chat",
        issue_id="issue-1",
        identifier="SYN-1",
        title="Plan",
        url="https://linear.example/issue/SYN-1",
        description="Description",
        updated_at="2026-07-31T00:00:00Z",
        public_source=True,
    )
    result = LinearPlanReviewResult(LinearPlanReviewDecision.PROCEED, "Looks good")
    review = AsyncMock(return_value=result)
    settings = MagicMock()
    settings.prompts.plan_freshness = "plan-freshness"
    settings.project_root = Path("/project")
    monkeypatch.setattr(app_module.linear_plan_review, "review_linear_plan", review)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "read_prompt", lambda name, root: f"prompt:{name}:{root}")
    monkeypatch.setattr(app_module, "resolve_learning_paths", lambda _folder: None)

    assert await app.review_linear_plan(request) is result
    assert app.host_runtime_operations.host_learning_vault("chat") is None

    review.assert_awaited_once_with(app, request, "prompt:plan-freshness:/project")


async def test_application_persists_and_replays_approval_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    decision = {"request_id": "request-1", "approved": True}
    deps = object()
    write_decision = Mock()
    process_decision = AsyncMock()
    configure_approval_state_root(tmp_path)
    monkeypatch.setattr(app_module, "write_json_atomic", write_decision)
    monkeypatch.setattr(app_module, "process_approval_decision", process_decision)

    await app.approval_runtime_operations.persist_and_process("chat", decision, deps)

    decision_path = tmp_path / "chat" / "approval_decisions" / "request-1.json"
    assert (tmp_path / "chat" / "approval_decisions").is_dir()
    write_decision.assert_called_once_with(decision_path, decision, indent=2)
    process_decision.assert_awaited_once_with(
        decision_path,
        "chat",
        deps=deps,
    )


def test_application_reports_no_live_session_when_runtime_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    monkeypatch.setattr(app_module, "get_session", lambda _folder: None)

    assert app.ask_user_runtime_operations.has_live_session("chat") is False


async def test_learning_review_drops_queued_callback_after_waiter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    run_agent = AsyncMock(return_value="late result")
    app.run_agent = run_agent
    callbacks: list[Callable[[], Awaitable[None]]] = []

    def enqueue_task(
        _target: object,
        _task_id: str,
        callback: Callable[[], Awaitable[None]],
    ) -> bool:
        callbacks.append(callback)
        return True

    async def review(
        _packet: LearningPacket,
        run_agent_via_queue: Callable[..., Awaitable[str]],
        _prompt: str,
    ) -> str:
        waiting = asyncio.create_task(run_agent_via_queue(group, "chat", []))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await callbacks[0]()
        return "cancelled"

    monkeypatch.setattr(app.queue, "enqueue_task", enqueue_task)
    monkeypatch.setattr(app_module, "run_host_learning_review", review)
    monkeypatch.setattr(app_module, "_read_current_prompt", lambda _: "review prompt")

    assert await app.run_learning_review(_learning_packet()) == "cancelled"
    run_agent.assert_not_awaited()
