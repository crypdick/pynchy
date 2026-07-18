"""Tests for the operational status collector and /status endpoint.

All subsystem behaviour is exercised through the public ``collect_status()``
entry point (and the ``/status`` HTTP endpoint), asserting on the observable
status dict rather than importing the private per-section collectors.
"""

from __future__ import annotations

import contextlib
import subprocess  # noqa: S404, RUF100 - test helpers mock subprocess behavior and exceptions
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase
from conftest import make_settings

from pynchy.canaries import declared_canary_actions
from pynchy.config.models import RepoConfig, ReposConfig
from pynchy.config.scheduler_models import SchedulerConfig
from pynchy.host.git_ops.repo import RepoContext
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.status import collect_status, record_start_time
from pynchy.plugins.speech import SpeechSynthesisResult, SpeechSynthesizerHealth
from pynchy.state import init_test_database
from pynchy.types import HostJob, ScheduledTask, TaskRunLog

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp import web

_S = "pynchy.host.orchestrator.status"

_EMPTY_STATS = {
    "total_inbound": 0,
    "total_outbound": 0,
    "last_received_at": None,
    "last_sent_at": None,
    "pending_deliveries": 0,
}


def _status_settings(*, repos: dict[str, RepoConfig] | None = None):
    return make_settings(
        repos=ReposConfig(overrides=repos or {}),
        scheduler=SchedulerConfig(),
    )


@contextlib.contextmanager
def _inert_status():
    """Neutralise every I/O-bound status collector via its public deps.

    Patches the external functions ``collect_status`` fans out to (git, DB,
    docker, HTTP) so a test can drive the single public entry point while only
    the section it cares about does real work. Tests layer their own ``patch``
    calls on top; the innermost patch wins.
    """
    with contextlib.ExitStack() as stack:

        def p(name: str, **kwargs: Any) -> None:
            stack.enter_context(patch(f"{_S}.{name}", **kwargs))

        p("get_head_sha", return_value="0000000")
        p("is_repo_dirty", return_value=False)
        p("count_unpushed_commits", return_value=0)
        p("get_head_commit_message", return_value="")
        p("get_router_state", new_callable=AsyncMock, return_value=None)
        p(
            "get_settings",
            return_value=_status_settings(),
        )
        p("get_messaging_stats", new_callable=AsyncMock, return_value=dict(_EMPTY_STATS))
        p(
            "get_canary_report",
            new_callable=AsyncMock,
            return_value={"summary": {"unresolved_regressions": 0}},
        )
        p(
            "collect_capability_status",
            new_callable=AsyncMock,
            return_value={"summary": {}, "workspaces": []},
        )
        p("get_all_tasks", new_callable=AsyncMock, return_value=[])
        p("get_task_run_logs", new_callable=AsyncMock, return_value=[])
        p("get_all_host_jobs", new_callable=AsyncMock, return_value=[])
        p("run_docker", new_callable=AsyncMock, return_value=Mock(returncode=1, stdout=""))
        p(
            "get_temporal_scheduler_status",
            create=True,
            return_value={
                "worker_running": False,
                "last_workflow_id": None,
                "last_task_id": None,
                "last_result": None,
                "last_started_at": None,
                "last_completed_at": None,
                "last_error": None,
            },
        )
        p(
            "_check_temporal_cluster_health",
            create=True,
            new_callable=AsyncMock,
            return_value={"healthy": None, "error": None},
        )
        stack.enter_context(patch("aiohttp.ClientSession", side_effect=Exception("skip")))
        yield


# ---------------------------------------------------------------------------
# Mock StatusDeps
# ---------------------------------------------------------------------------


class MockStatusDeps:
    """Mock implementation of StatusDeps for testing."""

    def __init__(
        self,
        *,
        shutting_down: bool = False,
        channels: dict[str, bool] | None = None,
        queue: dict[str, Any] | None = None,
        gateway: dict[str, Any] | None = None,
        active_sessions: int = 0,
        workspace_count: int = 0,
        speech_synthesizer: Any | None = None,
    ):
        self._shutting_down = shutting_down
        self._channels = channels or {"whatsapp": True}
        self._queue = queue or {
            "active_containers": 1,
            "max_concurrent": 10,
            "groups_waiting": 0,
            "per_group": {},
        }
        self._gateway = gateway or {"mode": "litellm", "port": 4000, "key": "sk-test"}
        self._active_sessions = active_sessions
        self._workspace_count = workspace_count
        self._speech_synthesizer = speech_synthesizer

    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def get_channel_status(self) -> dict[str, bool]:
        return self._channels

    def get_queue_snapshot(self) -> dict[str, Any]:
        return self._queue

    def get_gateway_info(self) -> dict[str, Any]:
        return self._gateway

    def get_active_sessions_count(self) -> int:
        return self._active_sessions

    def get_workspace_count(self) -> int:
        return self._workspace_count

    def get_speech_synthesizer(self) -> Any | None:
        return self._speech_synthesizer


class MockHttpDeps:
    """Inert HTTP dependencies for exercising route registration."""

    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        return None

    def admin_chat_jid(self) -> str:
        return "admin@g.us"

    def channels_connected(self) -> bool:
        return True

    def get_groups(self) -> list[dict[str, Any]]:
        return []

    async def get_messages(self, _jid: str, _limit: int) -> list[Any]:
        return []

    async def send_user_message(self, _jid: str, _content: str) -> None:
        return None

    def subscribe_events(self, _callback: Any) -> Any:
        return lambda: None

    async def get_periodic_agents(self) -> list[dict[str, Any]]:
        return []

    def get_active_sessions(self) -> dict[str, str]:
        return {}

    def is_shutting_down(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# service section
# ---------------------------------------------------------------------------


class TestCollectService:
    @pytest.mark.asyncio
    async def test_ok_status(self):
        deps = MockStatusDeps()
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


# ---------------------------------------------------------------------------
# deploy section
# ---------------------------------------------------------------------------


class TestCollectDeploy:
    @pytest.mark.asyncio
    async def test_assembles_deploy_info(self):
        deps = MockStatusDeps()
        with (
            _inert_status(),
            patch(f"{_S}.get_head_sha", return_value="abc123"),
            patch(f"{_S}.is_repo_dirty", return_value=False),
            patch(f"{_S}.count_unpushed_commits", return_value=0),
            patch(f"{_S}.get_head_commit_message", return_value="test commit"),
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


# ---------------------------------------------------------------------------
# repos section
# ---------------------------------------------------------------------------


class TestCollectRepos:
    @pytest.mark.asyncio
    async def test_repo_status(self, tmp_path: Path):
        """A tracked repo surfaces its head/dirty/unpushed status."""

        ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "worktrees")
        deps = MockStatusDeps()

        with (
            _inert_status(),
            patch(
                f"{_S}.get_settings",
                return_value=_status_settings(repos={"owner/repo": RepoConfig()}),
            ),
            patch(f"{_S}.get_repo_context", return_value=ctx),
            patch(f"{_S}.get_head_sha", return_value="def456"),
            patch(f"{_S}.is_repo_dirty", return_value=True),
            patch(f"{_S}.count_unpushed_commits", return_value=2),
        ):
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
        deps = MockStatusDeps()

        with (
            _inert_status(),
            patch(
                f"{_S}.get_settings",
                return_value=_status_settings(repos={"owner/repo": RepoConfig()}),
            ),
            patch(f"{_S}.get_repo_context", return_value=ctx),
            patch(f"{_S}.get_head_sha", return_value="aaa111"),
            patch(f"{_S}.is_repo_dirty", return_value=False),
            patch(f"{_S}.count_unpushed_commits", return_value=0),
            patch(f"{_S}.detect_main_branch", return_value="main"),
            patch(f"{_S}.run_git", side_effect=[mock_git, mock_git, mock_git_dir]),
        ):
            result = await collect_status(deps, time.monotonic())

        worktrees = result["repos"]["owner/repo"]["worktrees"]
        assert "code-improver" in worktrees
        wt = worktrees["code-improver"]
        assert wt["ahead"] == 3
        assert wt["behind"] == 3
        assert wt["conflict"] is False


# ---------------------------------------------------------------------------
# worktree status (conflict detection)
# ---------------------------------------------------------------------------


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
        deps = MockStatusDeps()

        with (
            _inert_status(),
            patch(
                f"{_S}.get_settings",
                return_value=_status_settings(repos={"owner/repo": RepoConfig()}),
            ),
            patch(f"{_S}.get_repo_context", return_value=ctx),
            patch(f"{_S}.get_head_sha", return_value="bbb222"),
            patch(f"{_S}.is_repo_dirty", return_value=True),
            patch(f"{_S}.count_unpushed_commits", return_value=0),
            patch(f"{_S}.detect_main_branch", return_value="main"),
            patch(f"{_S}.run_git", side_effect=[mock_ahead, mock_behind, mock_git_dir]),
        ):
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
        deps = MockStatusDeps()

        with (
            _inert_status(),
            patch(
                f"{_S}.get_settings",
                return_value=_status_settings(repos={"owner/repo": RepoConfig()}),
            ),
            patch(f"{_S}.get_repo_context", return_value=ctx),
            patch(f"{_S}.get_head_sha", return_value="ccc333"),
            patch(f"{_S}.is_repo_dirty", return_value=False),
            patch(f"{_S}.count_unpushed_commits", return_value=0),
            patch(f"{_S}.detect_main_branch", return_value="main"),
            patch(f"{_S}.run_git", side_effect=[mock_ahead, mock_behind, mock_git_dir]),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["repos"]["owner/repo"]["worktrees"]["wt1"]["conflict"] is False

    @pytest.mark.asyncio
    async def test_git_dir_failure_returns_no_conflict(self, tmp_path: Path):
        """If rev-parse --git-dir fails, conflict defaults to False."""
        ctx, _ = self._repo_ctx(tmp_path)

        mock_ahead = Mock(returncode=0, stdout="0\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=1, stdout="")
        deps = MockStatusDeps()

        with (
            _inert_status(),
            patch(
                f"{_S}.get_settings",
                return_value=_status_settings(repos={"owner/repo": RepoConfig()}),
            ),
            patch(f"{_S}.get_repo_context", return_value=ctx),
            patch(f"{_S}.get_head_sha", return_value="ddd444"),
            patch(f"{_S}.is_repo_dirty", return_value=False),
            patch(f"{_S}.count_unpushed_commits", return_value=0),
            patch(f"{_S}.detect_main_branch", return_value="main"),
            patch(f"{_S}.run_git", side_effect=[mock_ahead, mock_behind, mock_git_dir]),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["repos"]["owner/repo"]["worktrees"]["wt1"]["conflict"] is False


# ---------------------------------------------------------------------------
# messages section
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# tasks section
# ---------------------------------------------------------------------------


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
                context_mode="group",
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
    async def test_includes_run_health_from_task_attempt_ledger(self):
        fake_tasks = [
            ScheduledTask(
                id="t1",
                group_folder="admin",
                chat_jid="admin@g.us",
                prompt="check health",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                context_mode="group",
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
                temporal_attempt=3,
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
            "last_temporal_attempt": 3,
            "escalation_reason": "stagnation",
        }


# ---------------------------------------------------------------------------
# host jobs section
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# temporal section
# ---------------------------------------------------------------------------


class TestCollectTemporal:
    @pytest.mark.asyncio
    async def test_surfaces_config_cluster_health_and_worker_state(self):
        settings = make_settings(
            repos=ReposConfig(),
            scheduler=SchedulerConfig(
                temporal_address="temporal.internal:7233",
                temporal_namespace="pynchy",
                temporal_task_queue="pynchy-scheduler-prod",
            ),
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
        deps = MockStatusDeps()

        with (
            _inert_status(),
            patch(f"{_S}.get_settings", return_value=settings),
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


# ---------------------------------------------------------------------------
# gateway section
# ---------------------------------------------------------------------------


class TestCollectGateway:
    @pytest.mark.asyncio
    async def test_non_litellm_mode(self):
        deps = MockStatusDeps(gateway={"mode": "builtin"})
        with _inert_status():
            result = await collect_status(deps, time.monotonic())
        assert result["gateway"] == {"mode": "builtin"}

    @pytest.mark.asyncio
    async def test_litellm_container_status(self):
        deps = MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {
            "status": "healthy",
            "db": "connected",
            "litellm_version": "1.2.3",
        }
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                side_effect=[
                    Mock(returncode=0, stdout="running\n"),
                    Mock(returncode=0, stdout="running\n"),
                ],
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["litellm_container"] == "running"
        assert gateway["postgres_container"] == "running"
        assert gateway["ready"] is True
        assert gateway["database"] == "connected"
        assert gateway["litellm_version"] == "1.2.3"
        mock_session.get.assert_called_once_with(
            "http://localhost:4000/health/readiness",
            headers={"Authorization": "Bearer sk-test"},
            timeout=mock_session.get.call_args.kwargs["timeout"],
        )

    @pytest.mark.asyncio
    async def test_litellm_container_status_uses_runtime_namespace(self, monkeypatch):
        monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy-feature-test")
        deps = MockStatusDeps(gateway={"mode": "litellm"})

        with (
            _inert_status(),
            patch(f"{_S}.run_docker", new_callable=AsyncMock) as run_docker,
        ):
            run_docker.return_value = Mock(returncode=0, stdout="running\n")
            await collect_status(deps, time.monotonic())

        inspected = [call.args[3] for call in run_docker.await_args_list]
        assert inspected == [
            "pynchy-feature-test-litellm",
            "pynchy-feature-test-litellm-db",
        ]

    @pytest.mark.asyncio
    async def test_litellm_readiness_accepts_current_healthy_shape(self):
        deps = MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"status": "healthy", "db": "connected"}
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                side_effect=[
                    Mock(returncode=0, stdout="running\n"),
                    Mock(returncode=0, stdout="running\n"),
                ],
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["gateway"]["ready"] is True
        assert result["gateway"]["database"] == "connected"

    @pytest.mark.asyncio
    async def test_gateway_health_failure_returns_none(self):
        """When gateway HTTP check fails, model counts are None."""
        deps = MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})
        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                return_value=Mock(returncode=0, stdout="running\n"),
            ),
            # aiohttp.ClientSession stays inert (raises) → health check fails.
        ):
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["litellm_container"] == "running"
        assert gateway["ready"] is None

    @pytest.mark.asyncio
    async def test_missing_port_skips_health_check(self):
        """When port or key is missing, health check is skipped."""
        deps = MockStatusDeps(gateway={"mode": "litellm"})
        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                side_effect=[
                    Mock(returncode=0, stdout="running\n"),
                    Mock(returncode=0, stdout="stopped\n"),
                ],
            ),
        ):
            result = await collect_status(deps, time.monotonic())

        gateway = result["gateway"]
        assert gateway["litellm_container"] == "running"
        assert gateway["postgres_container"] == "stopped"
        assert "healthy_models" not in gateway


# ---------------------------------------------------------------------------
# container state (observed through the gateway section)
# ---------------------------------------------------------------------------


class TestContainerState:
    """Docker container state resolution, observed via gateway container fields."""

    @staticmethod
    def _litellm_deps() -> MockStatusDeps:
        return MockStatusDeps(gateway={"mode": "litellm", "port": 4000, "key": "sk-test"})

    @pytest.mark.asyncio
    async def test_running_container(self):
        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                return_value=Mock(returncode=0, stdout="running\n"),
            ),
        ):
            result = await collect_status(self._litellm_deps(), time.monotonic())
        assert result["gateway"]["litellm_container"] == "running"

    @pytest.mark.asyncio
    async def test_stopped_container(self):
        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                return_value=Mock(returncode=0, stdout="exited\n"),
            ),
        ):
            result = await collect_status(self._litellm_deps(), time.monotonic())
        assert result["gateway"]["litellm_container"] == "exited"

    @pytest.mark.asyncio
    async def test_not_found(self):
        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                return_value=Mock(returncode=1, stdout=""),
            ),
        ):
            result = await collect_status(self._litellm_deps(), time.monotonic())
        assert result["gateway"]["litellm_container"] == "not_found"

    @pytest.mark.asyncio
    async def test_docker_not_installed(self):
        with (
            _inert_status(),
            patch(f"{_S}.run_docker", new_callable=AsyncMock, side_effect=FileNotFoundError),
        ):
            result = await collect_status(self._litellm_deps(), time.monotonic())
        assert result["gateway"]["litellm_container"] == "not_found"

    @pytest.mark.asyncio
    async def test_docker_timeout(self):
        with (
            _inert_status(),
            patch(
                f"{_S}.run_docker",
                new_callable=AsyncMock,
                side_effect=subprocess.TimeoutExpired("docker", 5),
            ),
        ):
            result = await collect_status(self._litellm_deps(), time.monotonic())
        assert result["gateway"]["litellm_container"] == "not_found"


class TestCollectStatus:
    @pytest.mark.asyncio
    async def test_returns_all_sections(self):
        """Top-level collect_status assembles all subsystem sections."""
        deps = MockStatusDeps(
            channels={"whatsapp": True, "slack": False},
            workspace_count=5,
            active_sessions=2,
        )
        record_start_time()

        with (
            _inert_status(),
            patch(f"{_S}.get_head_sha", return_value="abc123"),
            patch(f"{_S}.get_head_commit_message", return_value="test"),
            patch(
                f"{_S}.get_messaging_stats",
                new_callable=AsyncMock,
                return_value={
                    "total_inbound": 100,
                    "total_outbound": 50,
                    "last_received_at": None,
                    "last_sent_at": None,
                    "pending_deliveries": 0,
                },
            ),
        ):
            result = await collect_status(deps, time.monotonic() - 120)

        expected_keys = {
            "service",
            "deploy",
            "channels",
            "gateway",
            "onecli",
            "queue",
            "repos",
            "messages",
            "tasks",
            "host_jobs",
            "temporal",
            "canaries",
            "capabilities",
            "speech",
            "groups",
        }
        assert set(result.keys()) == expected_keys

        # In-memory sections are passed through from deps
        assert result["channels"] == {"whatsapp": True, "slack": False}
        assert result["groups"]["total"] == 5
        assert result["groups"]["active_sessions"] == 2
        assert result["service"]["status"] == "ok"
        assert result["service"]["uptime_seconds"] >= 120
        assert result["messages"]["total_inbound"] == 100
        assert result["canaries"] == {"summary": {"unresolved_regressions": 0}}
        assert result["capabilities"] == {"summary": {}, "workspaces": []}

    @pytest.mark.asyncio
    async def test_includes_onecli_status(self):
        deps = MockStatusDeps()
        onecli = {"enabled": True, "ready": True, "egress_pending_approvals": 1}

        with (
            _inert_status(),
            patch(f"{_S}.collect_onecli_status", return_value=onecli),
        ):
            result = await collect_status(deps, time.monotonic())

        assert result["onecli"] == onecli


# ---------------------------------------------------------------------------
# /status HTTP endpoint
# ---------------------------------------------------------------------------


class TestStatusEndpoint(AioHTTPTestCase):
    """Tests for GET /status endpoint."""

    async def get_application(self) -> web.Application:
        self.mock_deps = MockStatusDeps(
            channels={"whatsapp": True},
            workspace_count=3,
            active_sessions=1,
        )
        return create_http_app(MockHttpDeps(), status_deps=self.mock_deps)

    async def test_status_returns_200(self):
        """GET /status returns 200 with structured JSON."""
        record_start_time()

        with _inert_status():
            resp = await self.client.get("/status")
            assert resp.status == 200
            data = await resp.json()
            assert "service" in data
            assert "deploy" in data
            assert "channels" in data
            assert "gateway" in data
            assert "queue" in data
            assert "groups" in data
            assert data["channels"] == {"whatsapp": True}

    async def test_work_items_endpoint_returns_empty_bounded_projection(self):
        await init_test_database()

        response = await self.client.get("/work-items?workspace=project&limit=1")
        invalid = await self.client.get("/work-items?limit=zero")

        assert response.status == 200
        assert await response.json() == {"workspace": "project", "work_items": []}
        assert invalid.status == 400

    async def test_canary_report_and_history_endpoints(self):
        await init_test_database()

        report_response = await self.client.get("/canaries/report")
        report = await report_response.json()
        history_response = await self.client.get("/canaries/runs?limit=1")
        history = await history_response.json()
        invalid_response = await self.client.get("/canaries/runs?limit=zero")

        assert report_response.status == 200
        assert report["summary"]["declared_scenarios"] == len(declared_canary_actions())
        assert history_response.status == 200
        assert history == {"runs": []}
        assert invalid_response.status == 400

    async def test_capabilities_endpoint_returns_all_or_one_workspace(self):
        all_payload = {"summary": {"ready": 1}, "workspaces": []}
        workspace_payload = {
            "workspace": "matrix",
            "summary": {"ready": 1},
            "capabilities": [],
        }
        snapshot = Mock()
        snapshot.to_dict.return_value = workspace_payload

        with (
            patch(
                "pynchy.host.orchestrator.http_server.get_canary_report",
                new_callable=AsyncMock,
                return_value={"scenarios": []},
            ),
            patch(
                "pynchy.host.orchestrator.http_server.collect_capability_status",
                new_callable=AsyncMock,
                return_value=all_payload,
            ),
            patch(
                "pynchy.host.orchestrator.http_server.resolve_workspace_capabilities",
                new_callable=AsyncMock,
                return_value=snapshot,
            ),
        ):
            all_response = await self.client.get("/capabilities")
            workspace_response = await self.client.get("/capabilities?workspace=matrix")

        assert all_response.status == 200
        assert await all_response.json() == all_payload
        assert workspace_response.status == 200
        assert await workspace_response.json() == workspace_payload
