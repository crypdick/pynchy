"""Curated Temporal API forwarding contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pynchy.deployments import DeployChangeKind, DeployClaim, DeployClaimStatus
from pynchy.host.orchestrator.temporal.api import (
    get_temporal_scheduler_runtime,
    get_temporal_scheduler_status,
    start_deploy_workflow,
    start_learning_review_workflow,
)
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.learning_packets import LearningPacket


def test_scheduler_runtime_is_resolved_lazily() -> None:
    assert get_temporal_scheduler_runtime().__name__ == "TemporalSchedulerRuntime"


def test_scheduler_status_forwards_to_runtime() -> None:
    with patch(
        "pynchy.host.orchestrator.temporal.scheduler.get_temporal_scheduler_status",
        return_value={"worker_running": True},
    ):
        assert get_temporal_scheduler_status() == {"worker_running": True}


async def test_deploy_workflow_forwards_request() -> None:
    claim = DeployClaim(DeployClaimStatus.CLAIMED, DeployChangeKind.CODE)
    request = DeployRequest(
        chat_jid="admin@g.us",
        commit_sha="abc",
        config_hash="config",
        previous_sha="old",
    )
    with patch(
        "pynchy.host.orchestrator.temporal.scheduler.start_deploy_workflow",
        new_callable=AsyncMock,
        return_value=claim,
    ) as start:
        assert await start_deploy_workflow(request) == claim

    start.assert_awaited_once_with(request)


async def test_learning_review_workflow_forwards_packet() -> None:
    packet = LearningPacket(
        job_id="job-1",
        chat_jid="admin@g.us",
        group_folder="admin",
        profile="default",
        created_at="2026-07-29T00:00:00Z",
        messages=[],
        final_answer=None,
        tool_counts={},
        error_snippets=[],
        loaded_skills=[],
        provenance={},
    )
    with patch(
        "pynchy.host.orchestrator.temporal.scheduler.start_learning_review_workflow",
        new_callable=AsyncMock,
    ) as start:
        assert await start_learning_review_workflow(packet) is None

    start.assert_awaited_once_with(packet)
