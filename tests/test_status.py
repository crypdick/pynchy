"""Tests for the operational status collector and /status endpoint.

All subsystem behaviour is exercised through the public ``collect_status()``
entry point (and the ``/status`` HTTP endpoint), asserting on the observable
status dict rather than importing the private per-section collectors.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pynchy.host.git_ops.api import RepoContext
from pynchy.host.orchestrator.status import collect_status, record_start_time
from pynchy.plugins.speech.api import SpeechSynthesisResult, SpeechSynthesizerHealth
from pynchy.scheduling.api import (
    HostJob,
    ScheduledTask,
    SessionPolicy,
    TaskRunLog,
)
from tests.status_support import (
    MockStatusDeps,
    _inert_status,
)

if TYPE_CHECKING:
    from pathlib import Path


_S = "pynchy.host.orchestrator.status"

_EMPTY_STATS = {
    "total_inbound": 0,
    "total_outbound": 0,
    "last_received_at": None,
    "last_sent_at": None,
    "pending_deliveries": 0,
}


class TestCollectService:
    @pytest.mark.asyncio
    async def test_ok_status(self):
        deps = MockStatusDeps(repo_slugs=("owner/repo",))
        with _inert_status():
            result = await collect_status(deps, time.monotonic() - 60)
        assert result["service"]["status"] == "ok"
        assert result["service"]["uptime_seconds"] >= 60
        assert "started_at" in result["service"]

    @pytest.mark.asyncio
    async def test_shutting_down_status(self):
        deps = MockStatusDeps(shutting_down=True)
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["service"]["status"] == "shutting_down"

    @pytest.mark.asyncio
    async def test_started_at_from_record(self):
        """record_start_time() sets the wall-clock start time."""
        record_start_time()
        deps = MockStatusDeps()
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["service"]["started_at"] is not None


class TestCollectSpeech:
    @pytest.mark.asyncio
    async def test_reports_provider_health(self):
        class ReadySynthesizer:
            name = "test-speech"

            async def synthesize(self, _text: str, _output_path: object) -> SpeechSynthesisResult:
                return SpeechSynthesisResult(success=False)

            async def health(self) -> SpeechSynthesizerHealth:
                return SpeechSynthesizerHealth(ready=True, endpoint="http://127.0.0.1:8000/")

        with _inert_status():
            result = await collect_status(
                MockStatusDeps(speech_synthesizer=ReadySynthesizer()), time.monotonic()
            )

        assert result["speech"] == {
            "provider": "test-speech",
            "ready": True,
            "endpoint": "http://127.0.0.1:8000/",
            "error": None,
        }


class TestCollectDeploy:
    @pytest.mark.asyncio
    async def test_assembles_deploy_info(self):
        deps = MockStatusDeps()
        deps.git_status.get_head_sha.return_value = "abc123"
        deps.git_status.is_repo_dirty.return_value = False
        deps.git_status.count_unpushed_commits.return_value = 0
        deps.git_status.get_head_commit_message.return_value = "test commit"
        with (
            _inert_status(),
            patch(
                f"{_S}.get_router_state",
                new_callable=AsyncMock,
                side_effect=["2026-02-20T09:00:00", "abc123"],
            ),
        ):
            result = await collect_status(deps, time.monotonic())

        deploy = result["deploy"]
        assert deploy["head_sha"] == "abc123"
        assert deploy["head_commit"] == "test commit"
        assert deploy["dirty"] is False
        assert deploy["unpushed_commits"] == 0
        assert deploy["last_deploy_at"] == "2026-02-20T09:00:00"
        assert deploy["last_deploy_sha"] == "abc123"


class TestCollectRepos:
    @pytest.mark.asyncio
    async def test_repo_status(self, tmp_path: Path):
        """A tracked repo surfaces its head/dirty/unpushed status."""

        ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "worktrees")
        deps = MockStatusDeps(repo_slugs=("owner/repo",))
        deps.git_status.get_repo_context.return_value = ctx
        deps.git_status.get_head_sha.return_value = "def456"
        deps.git_status.is_repo_dirty.return_value = True
        deps.git_status.count_unpushed_commits.return_value = 2

        with _inert_status():
            result = await collect_status(deps, time.monotonic())

        repo = result["repos"]["owner/repo"]
        assert repo["head_sha"] == "def456"
        assert repo["dirty"] is True
        assert repo["unpushed_commits"] == 2
        assert "worktrees" not in repo  # no worktrees dir

    @pytest.mark.asyncio
    async def test_repo_with_worktrees(self, tmp_path: Path):
        """A repo with worktrees surfaces per-worktree data."""
        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        (wt_dir / "code-improver").mkdir()

        ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=wt_dir)
        mock_git = Mock(returncode=0, stdout="3\n")
        mock_git_dir = Mock(returncode=0, stdout=str(tmp_path / ".git/worktrees/code-improver"))
        deps = MockStatusDeps(repo_slugs=("owner/repo",))
        deps.git_status.get_repo_context.return_value = ctx
        deps.git_status.get_head_sha.return_value = "aaa111"
        deps.git_status.is_repo_dirty.return_value = False
        deps.git_status.count_unpushed_commits.return_value = 0
        deps.git_status.detect_main_branch.return_value = "main"
        deps.git_status.run_git.side_effect = [mock_git, mock_git, mock_git_dir]

        with _inert_status():
            result = await collect_status(deps, time.monotonic())

        worktrees = result["repos"]["owner/repo"]["worktrees"]
        assert "code-improver" in worktrees
        wt = worktrees["code-improver"]
        assert wt["ahead"] == 3
        assert wt["behind"] == 3
        assert wt["conflict"] is False


class TestWorktreeStatus:
    @staticmethod
    def _repo_ctx(tmp_path: Path) -> tuple[RepoContext, Path]:
        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        (wt_dir / "wt1").mkdir()

        ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=wt_dir)
        return ctx, wt_dir

    @pytest.mark.asyncio
    async def test_conflict_detection(self, tmp_path: Path):
        """Detects merge conflicts via MERGE_HEAD in the worktree git dir."""
        ctx, _ = self._repo_ctx(tmp_path)
        git_dir = tmp_path / "fake_git_dir"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()

        mock_ahead = Mock(returncode=0, stdout="1\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=0, stdout=str(git_dir))
        deps = MockStatusDeps(repo_slugs=("owner/repo",))
        deps.git_status.get_repo_context.return_value = ctx
        deps.git_status.get_head_sha.return_value = "bbb222"
        deps.git_status.is_repo_dirty.return_value = True
        deps.git_status.count_unpushed_commits.return_value = 0
        deps.git_status.detect_main_branch.return_value = "main"
        deps.git_status.run_git.side_effect = [mock_ahead, mock_behind, mock_git_dir]

        with _inert_status():
            result = await collect_status(deps, time.monotonic())

        wt = result["repos"]["owner/repo"]["worktrees"]["wt1"]
        assert wt["conflict"] is True
        assert wt["sha"] == "bbb222"
        assert wt["dirty"] is True
        assert wt["ahead"] == 1
        assert wt["behind"] == 0

    @pytest.mark.asyncio
    async def test_no_conflict(self, tmp_path: Path):
        """No conflict when neither MERGE_HEAD nor REBASE_HEAD exists."""
        ctx, _ = self._repo_ctx(tmp_path)
        git_dir = tmp_path / "clean_git_dir"
        git_dir.mkdir()

        mock_ahead = Mock(returncode=0, stdout="0\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=0, stdout=str(git_dir))
        deps = MockStatusDeps(repo_slugs=("owner/repo",))
        deps.git_status.get_repo_context.return_value = ctx
        deps.git_status.get_head_sha.return_value = "ccc333"
        deps.git_status.is_repo_dirty.return_value = False
        deps.git_status.count_unpushed_commits.return_value = 0
        deps.git_status.detect_main_branch.return_value = "main"
        deps.git_status.run_git.side_effect = [mock_ahead, mock_behind, mock_git_dir]

        with _inert_status():
            result = await collect_status(deps, time.monotonic())

        assert result["repos"]["owner/repo"]["worktrees"]["wt1"]["conflict"] is False

    @pytest.mark.asyncio
    async def test_git_dir_failure_returns_no_conflict(self, tmp_path: Path):
        """If rev-parse --git-dir fails, conflict defaults to False."""
        ctx, _ = self._repo_ctx(tmp_path)

        mock_ahead = Mock(returncode=0, stdout="0\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=1, stdout="")
        deps = MockStatusDeps(repo_slugs=("owner/repo",))
        deps.git_status.get_repo_context.return_value = ctx
        deps.git_status.get_head_sha.return_value = "ddd444"
        deps.git_status.is_repo_dirty.return_value = False
        deps.git_status.count_unpushed_commits.return_value = 0
        deps.git_status.detect_main_branch.return_value = "main"
        deps.git_status.run_git.side_effect = [mock_ahead, mock_behind, mock_git_dir]

        with _inert_status():
            result = await collect_status(deps, time.monotonic())

        assert result["repos"]["owner/repo"]["worktrees"]["wt1"]["conflict"] is False


class TestCollectMessages:
    """The status collector surfaces get_messaging_stats() under 'messages'.

    The DB-counting logic of get_messaging_stats itself is covered in
    tests/test_state.py; here we only assert the status collector passes it
    through unchanged.
    """

    @pytest.mark.asyncio
    async def test_surfaces_message_stats(self):
        stats = {
            "total_inbound": 1,
            "total_outbound": 1,
            "last_received_at": "2026-02-20T10:00:00",
            "last_sent_at": "2026-02-20T10:00:01",
            "pending_deliveries": 0,
        }
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_messaging_stats", new_callable=AsyncMock, return_value=stats),
        ):
            result = await collect_status(deps, time.monotonic())
        assert result["messages"] == stats

    @pytest.mark.asyncio
    async def test_empty_stats_pass_through(self):
        deps = MockStatusDeps()
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["messages"]["total_inbound"] == 0
        assert result["messages"]["total_outbound"] == 0
        assert result["messages"]["last_received_at"] is None
        assert result["messages"]["last_sent_at"] is None
        assert result["messages"]["pending_deliveries"] == 0


class TestCollectTasks:
    @pytest.mark.asyncio
    async def test_returns_task_list(self):
        fake_tasks = [
            ScheduledTask(
                id="t1",
                group_folder="admin",
                chat_jid="admin@g.us",
                prompt="check health",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                session_policy=SessionPolicy.CONTINUE,
                status="active",
                next_run="2026-02-21T09:00:00",
                last_run="2026-02-20T09:00:00",
                last_result="ok",
            ),
        ]
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_all_tasks", new_callable=AsyncMock, return_value=fake_tasks),
        ):
            result = await collect_status(deps, time.monotonic())

        tasks = result["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["id"] == "t1"
        assert tasks[0]["group"] == "admin"
        assert tasks[0]["schedule_type"] == "cron"
        assert tasks[0]["status"] == "active"
        assert tasks[0]["last_result"] == "ok"

    @pytest.mark.asyncio
    async def test_uses_temporal_next_run_instead_of_stale_database_value(self):
        task = ScheduledTask(
            id="t1",
            group_folder="admin",
            chat_jid="admin@g.us",
            prompt="check health",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            session_policy=SessionPolicy.CONTINUE,
            status="active",
            next_run="2026-02-21T09:00:00+00:00",
        )
        temporal_state = {
            ("task", "t1"): {
                "source": "temporal",
                "state": "scheduled",
                "next_run": "2026-02-21T17:00:00+00:00",
                "schedule_id": "pynchy-agent-schedule-t1",
                "workflow_id": None,
                "error": None,
            }
        }
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_all_tasks", new_callable=AsyncMock, return_value=[task]),
            patch(
                f"{_S}._get_temporal_orchestration_states",
                new_callable=AsyncMock,
                return_value=temporal_state,
            ),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["tasks"][0]["next_run"] == "2026-02-21T17:00:00+00:00"
        assert result["tasks"][0]["orchestration"]["source"] == "temporal"

    @pytest.mark.asyncio
    async def test_reports_unavailable_temporal_state_without_database_fallback(self):
        task = ScheduledTask(
            id="t1",
            group_folder="admin",
            chat_jid="admin@g.us",
            prompt="check health",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            session_policy=SessionPolicy.CONTINUE,
            status="active",
            next_run="2026-02-21T09:00:00+00:00",
        )
        temporal_state = {
            ("task", "t1"): {
                "source": "temporal",
                "state": "unavailable",
                "next_run": None,
                "schedule_id": "pynchy-agent-schedule-t1",
                "workflow_id": None,
                "error": "connection refused",
            }
        }
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_all_tasks", new_callable=AsyncMock, return_value=[task]),
            patch(
                f"{_S}._get_temporal_orchestration_states",
                new_callable=AsyncMock,
                return_value=temporal_state,
            ),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["tasks"][0]["next_run"] is None
        assert result["tasks"][0]["orchestration"]["state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_includes_run_health_from_task_attempt_ledger(self):
        fake_tasks = [
            ScheduledTask(
                id="t1",
                group_folder="admin",
                chat_jid="admin@g.us",
                prompt="check health",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                session_policy=SessionPolicy.CONTINUE,
                status="paused",
            ),
        ]
        fake_logs = [
            TaskRunLog(
                task_id="t1",
                run_at="2026-02-21T09:02:00+00:00",
                duration_ms=100,
                status="error",
                error="Same error repeated 3 times in a row",
                temporal_workflow_id="workflow-1",
                temporal_workflow_run_id="workflow-run-1",
                temporal_attempt=3,
                turn_id="turn-1",
                error_signature="RuntimeError: same failure",
                escalation_reason="stagnation",
            ),
            TaskRunLog(
                task_id="t1",
                run_at="2026-02-21T09:01:00+00:00",
                duration_ms=100,
                status="error",
                error="RuntimeError: same failure",
                error_signature="RuntimeError: same failure",
            ),
            TaskRunLog(
                task_id="t1",
                run_at="2026-02-21T09:00:00+00:00",
                duration_ms=100,
                status="success",
                result="ok",
            ),
        ]
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_all_tasks", new_callable=AsyncMock, return_value=fake_tasks),
            patch(f"{_S}.get_task_run_logs", new_callable=AsyncMock, return_value=fake_logs),
        ):
            result = await collect_status(deps, time.monotonic())

        run_health = result["tasks"][0]["run_health"]
        assert run_health == {
            "last_status": "error",
            "consecutive_failures": 2,
            "last_error_signature": "RuntimeError: same failure",
            "last_temporal_workflow_id": "workflow-1",
            "last_temporal_workflow_run_id": "workflow-run-1",
            "last_temporal_attempt": 3,
            "last_turn_id": "turn-1",
            "escalation_reason": "stagnation",
        }


class TestCollectHostJobs:
    @pytest.mark.asyncio
    async def test_returns_job_list(self):
        fake_jobs = [
            HostJob(
                id="j1",
                name="backup-db",
                command="tar czf backup.tar.gz db/",
                schedule_type="cron",
                schedule_value="0 3 * * *",
                created_by="admin",
                status="active",
                enabled=True,
                next_run="2026-02-21T03:00:00",
                last_run="2026-02-20T03:00:00",
            ),
        ]
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_all_host_jobs", new_callable=AsyncMock, return_value=fake_jobs),
        ):
            result = await collect_status(deps, time.monotonic())

        jobs = result["host_jobs"]
        assert len(jobs) == 1
        assert jobs[0]["id"] == "j1"
        assert jobs[0]["name"] == "backup-db"
        assert jobs[0]["enabled"] is True


class TestCollectTemporal:
    @pytest.mark.asyncio
    async def test_surfaces_config_cluster_health_and_worker_state(self):
        deps = MockStatusDeps(
            temporal_address="temporal.internal:7233",
            temporal_namespace="pynchy",
            temporal_task_queue="pynchy-scheduler-prod",
        )
        worker = {
            "worker_running": True,
            "last_workflow_id": "pynchy-agent-task-health-2026-07-07T17-30-00Z",
            "last_task_id": "health",
            "last_result": "completed",
            "last_started_at": "2026-07-07T17:30:01+00:00",
            "last_completed_at": "2026-07-07T17:30:05+00:00",
            "last_error": None,
        }
        with (
            _inert_status(),
            patch(f"{_S}.get_temporal_scheduler_status", create=True, return_value=worker),
            patch(
                f"{_S}._check_temporal_cluster_health",
                create=True,
                new_callable=AsyncMock,
                return_value={"healthy": True, "error": None},
            ),
        ):
            result = await collect_status(deps, time.monotonic())

        temporal = result["temporal"]
        assert temporal == {
            "address": "temporal.internal:7233",
            "namespace": "pynchy",
            "task_queue": "pynchy-scheduler-prod",
            "cluster_healthy": True,
            "cluster_error": None,
            "worker_running": True,
            "last_workflow_id": "pynchy-agent-task-health-2026-07-07T17-30-00Z",
            "last_task_id": "health",
            "last_result": "completed",
            "last_started_at": "2026-07-07T17:30:01+00:00",
            "last_completed_at": "2026-07-07T17:30:05+00:00",
            "last_error": None,
        }

    @pytest.mark.asyncio
    async def test_temporal_cluster_error_keeps_worker_state_visible(self):
        deps = MockStatusDeps()
        worker = {
            "worker_running": True,
            "last_workflow_id": "wf-1",
            "last_task_id": "task-1",
            "last_result": "started",
            "last_started_at": "2026-07-07T17:31:01+00:00",
            "last_completed_at": None,
            "last_error": None,
        }

        with (
            _inert_status(),
            patch(f"{_S}.get_temporal_scheduler_status", create=True, return_value=worker),
            patch(
                f"{_S}._check_temporal_cluster_health",
                create=True,
                new_callable=AsyncMock,
                return_value={"healthy": None, "error": "connection refused"},
            ),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["temporal"]["cluster_healthy"] is None
        assert result["temporal"]["cluster_error"] == "connection refused"
        assert result["temporal"]["worker_running"] is True
        assert result["temporal"]["last_workflow_id"] == "wf-1"
