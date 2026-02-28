"""Tests for the operational status collector and /status endpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from pynchy.host.orchestrator.http_server import status_deps_key
from pynchy.host.orchestrator.status import (
    _collect_deploy,
    _collect_gateway,
    _collect_service,
    _container_state,
    collect_status,
    record_start_time,
)

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


# ---------------------------------------------------------------------------
# _collect_service
# ---------------------------------------------------------------------------


class TestCollectService:
    def test_ok_status(self):
        deps = MockStatusDeps()
        result = _collect_service(deps, time.monotonic() - 60)
        assert result["status"] == "ok"
        assert result["uptime_seconds"] >= 60
        assert "started_at" in result

    def test_shutting_down_status(self):
        deps = MockStatusDeps(shutting_down=True)
        result = _collect_service(deps, time.monotonic())
        assert result["status"] == "shutting_down"

    def test_started_at_from_record(self):
        """record_start_time() sets the wall-clock start time."""
        record_start_time()
        deps = MockStatusDeps()
        result = _collect_service(deps, time.monotonic())
        assert result["started_at"] is not None


# ---------------------------------------------------------------------------
# _collect_deploy
# ---------------------------------------------------------------------------


class TestCollectDeploy:
    @pytest.mark.asyncio
    async def test_assembles_deploy_info(self):
        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="abc123"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=False),
            patch("pynchy.host.orchestrator.status.count_unpushed_commits", return_value=0),
            patch("pynchy.host.orchestrator.status.get_head_commit_message", return_value="test commit"),
            patch("pynchy.host.orchestrator.status.get_router_state", side_effect=["2026-02-20T09:00:00", "abc123"]),
        ):
            result = await _collect_deploy()
            assert result["head_sha"] == "abc123"
            assert result["head_commit"] == "test commit"
            assert result["dirty"] is False
            assert result["unpushed_commits"] == 0
            assert result["last_deploy_at"] == "2026-02-20T09:00:00"
            assert result["last_deploy_sha"] == "abc123"


# ---------------------------------------------------------------------------
# _collect_repos
# ---------------------------------------------------------------------------


class TestCollectRepos:
    def test_repo_status(self, tmp_path: Path):
        """_collect_repos returns per-repo status."""
        from pynchy.host.orchestrator.status import _repo_status

        @dataclass
        class FakeRepoCtx:
            slug: str = "owner/repo"
            root: Path = tmp_path
            worktrees_dir: Path = tmp_path / "worktrees"

        ctx = FakeRepoCtx()

        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="def456"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=True),
            patch("pynchy.host.orchestrator.status.count_unpushed_commits", return_value=2),
        ):
            result = _repo_status(ctx)
            assert result["head_sha"] == "def456"
            assert result["dirty"] is True
            assert result["unpushed_commits"] == 2
            assert "worktrees" not in result  # no worktrees dir

    def test_repo_with_worktrees(self, tmp_path: Path):
        """_repo_status includes worktree data when worktrees exist."""
        from pynchy.host.orchestrator.status import _repo_status

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        (wt_dir / "code-improver").mkdir()

        @dataclass
        class FakeRepoCtx:
            slug: str = "owner/repo"
            root: Path = tmp_path
            worktrees_dir: Path = wt_dir

        ctx = FakeRepoCtx()

        mock_git = Mock(returncode=0, stdout="3\n")
        mock_git_dir = Mock(returncode=0, stdout=str(tmp_path / ".git/worktrees/code-improver"))

        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="aaa111"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=False),
            patch("pynchy.host.orchestrator.status.count_unpushed_commits", return_value=0),
            patch("pynchy.host.orchestrator.status.detect_main_branch", return_value="main"),
            patch("pynchy.host.orchestrator.status.run_git", side_effect=[mock_git, mock_git, mock_git_dir]),
        ):
            result = _repo_status(ctx)
            assert "worktrees" in result
            assert "code-improver" in result["worktrees"]
            wt = result["worktrees"]["code-improver"]
            assert wt["ahead"] == 3
            assert wt["behind"] == 3
            assert wt["conflict"] is False


# ---------------------------------------------------------------------------
# _worktree_status
# ---------------------------------------------------------------------------


class TestWorktreeStatus:
    def test_conflict_detection(self, tmp_path: Path):
        """Detects merge conflicts via MERGE_HEAD in git dir."""
        from pynchy.host.orchestrator.status import _worktree_status

        # Create a fake git dir with MERGE_HEAD
        git_dir = tmp_path / "fake_git_dir"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()

        mock_ahead = Mock(returncode=0, stdout="1\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=0, stdout=str(git_dir))

        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="bbb222"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=True),
            patch("pynchy.host.orchestrator.status.run_git", side_effect=[mock_ahead, mock_behind, mock_git_dir]),
        ):
            result = _worktree_status(tmp_path, "main", tmp_path.parent)
            assert result["conflict"] is True
            assert result["sha"] == "bbb222"
            assert result["dirty"] is True
            assert result["ahead"] == 1
            assert result["behind"] == 0

    def test_no_conflict(self, tmp_path: Path):
        """No conflict when neither MERGE_HEAD nor REBASE_HEAD exists."""
        from pynchy.host.orchestrator.status import _worktree_status

        git_dir = tmp_path / "clean_git_dir"
        git_dir.mkdir()

        mock_ahead = Mock(returncode=0, stdout="0\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=0, stdout=str(git_dir))

        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="ccc333"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=False),
            patch("pynchy.host.orchestrator.status.run_git", side_effect=[mock_ahead, mock_behind, mock_git_dir]),
        ):
            result = _worktree_status(tmp_path, "main", tmp_path.parent)
            assert result["conflict"] is False

    def test_git_dir_failure_returns_no_conflict(self, tmp_path: Path):
        """If rev-parse --git-dir fails, conflict defaults to False."""
        from pynchy.host.orchestrator.status import _worktree_status

        mock_ahead = Mock(returncode=0, stdout="0\n")
        mock_behind = Mock(returncode=0, stdout="0\n")
        mock_git_dir = Mock(returncode=1, stdout="")

        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="ddd444"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=False),
            patch("pynchy.host.orchestrator.status.run_git", side_effect=[mock_ahead, mock_behind, mock_git_dir]),
        ):
            result = _worktree_status(tmp_path, "main", tmp_path.parent)
            assert result["conflict"] is False


# ---------------------------------------------------------------------------
# _collect_messages (requires test DB)
# ---------------------------------------------------------------------------


class TestCollectMessages:
    @pytest.mark.asyncio
    async def test_returns_message_stats(self):
        """_collect_messages returns counts and timestamps from the DB."""
        from pynchy.state.connection import _init_test_database
        from pynchy.host.orchestrator.status import _collect_messages

        await _init_test_database()

        from pynchy.state.connection import _get_db

        db = _get_db()
        # FK requires a chat row first
        await db.execute(
            "INSERT OR IGNORE INTO chats (jid, name) VALUES (?, ?)", ("g@g.us", "Test")
        )
        # Insert test inbound message
        await db.execute(
            "INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("m1", "g@g.us", "u@s", "Alice", "hello", "2026-02-20T10:00:00", 0),
        )
        # Insert test outbound ledger entry
        await db.execute(
            "INSERT INTO outbound_ledger (chat_jid, content, timestamp, source) "
            "VALUES (?, ?, ?, ?)",
            ("g@g.us", "hi back", "2026-02-20T10:00:01", "test"),
        )
        await db.commit()

        result = await _collect_messages()
        assert result["total_inbound"] == 1
        assert result["total_outbound"] == 1
        assert result["last_received_at"] == "2026-02-20T10:00:00"
        assert result["last_sent_at"] == "2026-02-20T10:00:01"
        assert result["pending_deliveries"] == 0

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self):
        """_collect_messages handles empty tables gracefully."""
        from pynchy.state.connection import _init_test_database
        from pynchy.host.orchestrator.status import _collect_messages

        await _init_test_database()

        result = await _collect_messages()
        assert result["total_inbound"] == 0
        assert result["total_outbound"] == 0
        assert result["last_received_at"] is None
        assert result["last_sent_at"] is None
        assert result["pending_deliveries"] == 0


# ---------------------------------------------------------------------------
# _collect_tasks
# ---------------------------------------------------------------------------


class TestCollectTasks:
    @pytest.mark.asyncio
    async def test_returns_task_list(self):
        from pynchy.host.orchestrator.status import _collect_tasks
        from pynchy.types import ScheduledTask

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

        with patch("pynchy.host.orchestrator.status.get_all_tasks", return_value=fake_tasks):
            result = await _collect_tasks()
            assert len(result) == 1
            assert result[0]["id"] == "t1"
            assert result[0]["group"] == "admin"
            assert result[0]["schedule_type"] == "cron"
            assert result[0]["status"] == "active"
            assert result[0]["last_result"] == "ok"


# ---------------------------------------------------------------------------
# _collect_host_jobs
# ---------------------------------------------------------------------------


class TestCollectHostJobs:
    @pytest.mark.asyncio
    async def test_returns_job_list(self):
        from pynchy.host.orchestrator.status import _collect_host_jobs
        from pynchy.types import HostJob

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

        with patch("pynchy.host.orchestrator.status.get_all_host_jobs", return_value=fake_jobs):
            result = await _collect_host_jobs()
            assert len(result) == 1
            assert result[0]["id"] == "j1"
            assert result[0]["name"] == "backup-db"
            assert result[0]["enabled"] is True


# ---------------------------------------------------------------------------
# _collect_gateway
# ---------------------------------------------------------------------------


class TestCollectGateway:
    @pytest.mark.asyncio
    async def test_non_litellm_mode(self):
        result = await _collect_gateway({"mode": "builtin"})
        assert result == {"mode": "builtin"}

    @pytest.mark.asyncio
    async def test_litellm_container_status(self):
        with patch(
            "pynchy.host.orchestrator.status._container_state",
            new_callable=AsyncMock,
            side_effect=["running", "running"],
        ):
            mock_resp = AsyncMock()
            mock_resp.json.return_value = {"healthy_count": 2, "unhealthy_count": 0}

            mock_session = AsyncMock()
            mock_session.get.return_value = mock_resp
            # Make it work as async context manager
            mock_session.__aenter__.return_value = mock_session
            mock_session.__aexit__.return_value = None

            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _collect_gateway({"mode": "litellm", "port": 4000, "key": "sk-test"})
                assert result["litellm_container"] == "running"
                assert result["postgres_container"] == "running"
                assert result["healthy_models"] == 2
                assert result["unhealthy_models"] == 0

    @pytest.mark.asyncio
    async def test_gateway_health_failure_returns_none(self):
        """When gateway HTTP check fails, model counts are None."""
        with (
            patch(
                "pynchy.host.orchestrator.status._container_state",
                new_callable=AsyncMock,
                side_effect=["running", "running"],
            ),
            patch("aiohttp.ClientSession", side_effect=Exception("connection refused")),
        ):
            result = await _collect_gateway({"mode": "litellm", "port": 4000, "key": "sk-test"})
            assert result["litellm_container"] == "running"
            assert result["healthy_models"] is None
            assert result["unhealthy_models"] is None

    @pytest.mark.asyncio
    async def test_missing_port_skips_health_check(self):
        """When port or key is missing, health check is skipped."""
        with patch(
            "pynchy.host.orchestrator.status._container_state",
            new_callable=AsyncMock,
            side_effect=["running", "stopped"],
        ):
            result = await _collect_gateway({"mode": "litellm"})
            assert result["litellm_container"] == "running"
            assert result["postgres_container"] == "stopped"
            assert "healthy_models" not in result


# ---------------------------------------------------------------------------
# _container_state
# ---------------------------------------------------------------------------


class TestContainerState:
    @pytest.mark.asyncio
    async def test_running_container(self):
        with patch("pynchy.host.orchestrator.status.run_docker", new_callable=AsyncMock) as mock:
            mock.return_value = Mock(returncode=0, stdout="running\n")
            assert await _container_state("pynchy-litellm") == "running"

    @pytest.mark.asyncio
    async def test_stopped_container(self):
        with patch("pynchy.host.orchestrator.status.run_docker", new_callable=AsyncMock) as mock:
            mock.return_value = Mock(returncode=0, stdout="exited\n")
            assert await _container_state("pynchy-litellm") == "exited"

    @pytest.mark.asyncio
    async def test_not_found(self):
        with patch("pynchy.host.orchestrator.status.run_docker", new_callable=AsyncMock) as mock:
            mock.return_value = Mock(returncode=1, stdout="")
            assert await _container_state("missing") == "not_found"

    @pytest.mark.asyncio
    async def test_docker_not_installed(self):
        with patch(
            "pynchy.host.orchestrator.status.run_docker", new_callable=AsyncMock, side_effect=FileNotFoundError
        ):
            assert await _container_state("any") == "not_found"

    @pytest.mark.asyncio
    async def test_docker_timeout(self):
        import subprocess

        with patch(
            "pynchy.host.orchestrator.status.run_docker",
            new_callable=AsyncMock,
            side_effect=subprocess.TimeoutExpired("docker", 5),
        ):
            assert await _container_state("any") == "not_found"


# ---------------------------------------------------------------------------
# collect_status (orchestrator)
# ---------------------------------------------------------------------------


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
            # Deploy
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="abc123"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=False),
            patch("pynchy.host.orchestrator.status.count_unpushed_commits", return_value=0),
            patch("pynchy.host.orchestrator.status.get_head_commit_message", return_value="test"),
            patch("pynchy.host.orchestrator.status.get_router_state", return_value=None),
            # Repos
            patch("pynchy.host.orchestrator.status._collect_repos", return_value={}),
            # Messages
            patch(
                "pynchy.host.orchestrator.status._collect_messages",
                return_value={
                    "total_inbound": 100,
                    "total_outbound": 50,
                    "last_received_at": None,
                    "last_sent_at": None,
                    "pending_deliveries": 0,
                },
            ),
            # Tasks
            patch("pynchy.host.orchestrator.status.get_all_tasks", return_value=[]),
            # Host jobs
            patch("pynchy.host.orchestrator.status.get_all_host_jobs", return_value=[]),
            # Gateway
            patch("pynchy.host.orchestrator.status._container_state", new_callable=AsyncMock, return_value="running"),
            patch("aiohttp.ClientSession", side_effect=Exception("skip")),
        ):
            result = await collect_status(deps, time.monotonic() - 120)

        # Verify all top-level keys exist
        expected_keys = {
            "service",
            "deploy",
            "channels",
            "gateway",
            "queue",
            "repos",
            "messages",
            "tasks",
            "host_jobs",
            "groups",
        }
        assert set(result.keys()) == expected_keys

        # Verify in-memory sections are passed through from deps
        assert result["channels"] == {"whatsapp": True, "slack": False}
        assert result["groups"]["total"] == 5
        assert result["groups"]["active_sessions"] == 2
        assert result["service"]["status"] == "ok"
        assert result["service"]["uptime_seconds"] >= 120


# ---------------------------------------------------------------------------
# /status HTTP endpoint
# ---------------------------------------------------------------------------


class TestStatusEndpoint(AioHTTPTestCase):
    """Tests for GET /status endpoint."""

    async def get_application(self) -> web.Application:
        from pynchy.host.orchestrator.http_server import _handle_status

        app = web.Application()
        self.mock_deps = MockStatusDeps(
            channels={"whatsapp": True},
            workspace_count=3,
            active_sessions=1,
        )
        app[status_deps_key] = self.mock_deps
        app.router.add_get("/status", _handle_status)
        return app

    async def test_status_returns_200(self):
        """GET /status returns 200 with structured JSON."""
        record_start_time()

        with (
            patch("pynchy.host.orchestrator.status.get_head_sha", return_value="abc123"),
            patch("pynchy.host.orchestrator.status.is_repo_dirty", return_value=False),
            patch("pynchy.host.orchestrator.status.count_unpushed_commits", return_value=0),
            patch("pynchy.host.orchestrator.status.get_head_commit_message", return_value="test"),
            patch("pynchy.host.orchestrator.status.get_router_state", return_value=None),
            patch("pynchy.host.orchestrator.status._collect_repos", return_value={}),
            patch(
                "pynchy.host.orchestrator.status._collect_messages",
                return_value={
                    "total_inbound": 0,
                    "total_outbound": 0,
                    "last_received_at": None,
                    "last_sent_at": None,
                    "pending_deliveries": 0,
                },
            ),
            patch("pynchy.host.orchestrator.status.get_all_tasks", return_value=[]),
            patch("pynchy.host.orchestrator.status.get_all_host_jobs", return_value=[]),
            patch(
                "pynchy.host.orchestrator.status._container_state", new_callable=AsyncMock, return_value="not_found"
            ),
            patch("aiohttp.ClientSession", side_effect=Exception("skip")),
        ):
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
