"""Tests for HTTP server endpoints and utilities."""

from __future__ import annotations

import contextlib
import json
import subprocess  # noqa: S404, RUF100 - test helpers mock subprocess behavior and exceptions
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from conftest import make_settings

from pynchy.host.git_ops.utils import (
    get_head_commit_message,
    get_head_sha,
    is_repo_dirty,
    push_local_commits,
)
from pynchy.host.orchestrator.http_control import ControlPlaneRuntime, RequestRateLimiter
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.types import DeployClaim, DeployClaimStatus, NewMessage

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp import web


def _cp(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Fake a run_git() return value with a real CompletedProcess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@contextlib.contextmanager
def _patch_settings(*, data_dir: Path, **overrides: Any):
    s = make_settings(data_dir=data_dir, **overrides)
    with patch("pynchy.host.orchestrator.http_server.get_settings", return_value=s):
        yield


# ---------------------------------------------------------------------------
# Git utility tests
# ---------------------------------------------------------------------------


def test_get_head_sha_success():
    """get_head_sha returns SHA when git succeeds."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(
            returncode=0,
            stdout="head-sha-001\n",
        )
        assert get_head_sha() == "head-sha-001"


def test_get_head_sha_failure():
    """get_head_sha returns 'unknown' when git fails."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(returncode=1, stdout="")
        assert get_head_sha() == "unknown"


def test_is_repo_dirty_clean():
    """is_repo_dirty returns False when no uncommitted changes."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(returncode=0, stdout="")
        assert is_repo_dirty() is False


def test_is_repo_dirty_has_changes():
    """is_repo_dirty returns True when uncommitted changes exist."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(
            returncode=0,
            stdout=" M src/pynchy/app.py\n?? newfile.txt\n",
        )
        assert is_repo_dirty() is True


def test_is_repo_dirty_failure():
    """is_repo_dirty returns False when git fails."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(returncode=1, stdout="")
        assert is_repo_dirty() is False


def test_get_head_commit_message_success():
    """get_head_commit_message returns commit subject."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _cp(
            returncode=0,
            stdout="Add feature X\n",
        )
        assert get_head_commit_message() == "Add feature X"


def test_get_head_commit_message_truncation():
    """get_head_commit_message truncates long subjects."""
    long_msg = "A" * 80
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _cp(returncode=0, stdout=f"{long_msg}\n")
        result = get_head_commit_message(max_length=72)
        assert len(result) == 72
        assert result.endswith("…")


def test_get_head_commit_message_failure():
    """get_head_commit_message returns empty string on failure."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _cp(returncode=1, stdout="")
        assert not get_head_commit_message()


def test_get_head_commit_message_exception():
    """get_head_commit_message returns empty string when subprocess raises."""
    with patch("subprocess.run", side_effect=OSError):
        assert not get_head_commit_message()


# ---------------------------------------------------------------------------
# Push local commits tests
# ---------------------------------------------------------------------------


def testpush_local_commits_nothing_to_push():
    """push_local_commits returns True when no local commits exist."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="0\n"),  # rev-list
        ]
        assert push_local_commits() is True


def testpush_local_commits_success():
    """push_local_commits returns True when push succeeds."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="3\n"),  # rev-list (3 commits)
            _cp(returncode=0),  # rebase
            _cp(returncode=0),  # push
        ]
        assert push_local_commits() is True


def testpush_local_commits_fetch_failure():
    """push_local_commits returns False when fetch fails."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _cp(returncode=1, stderr="network error", stdout="")
        assert push_local_commits() is False


def testpush_local_commits_rebase_failure_retries_and_fails():
    """push_local_commits retries once after rebase failure, then gives up."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="2\n"),  # rev-list
            _cp(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 1)
            _cp(returncode=0),  # rebase --abort
            _cp(returncode=0),  # retry fetch
            _cp(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 2)
            _cp(returncode=0),  # rebase --abort
        ]
        assert push_local_commits() is False


def testpush_local_commits_rebase_retry_succeeds():
    """push_local_commits succeeds on retry when origin advanced mid-push."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="2\n"),  # rev-list
            _cp(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 1)
            _cp(returncode=0),  # rebase --abort
            _cp(returncode=0),  # retry fetch
            _cp(returncode=0),  # rebase succeeds (attempt 2)
            _cp(returncode=0),  # push
        ]
        assert push_local_commits() is True


def testpush_local_commits_push_failure():
    """push_local_commits returns False when push is rejected."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="1\n"),  # rev-list
            _cp(returncode=0),  # rebase
            _cp(returncode=1, stderr="rejected"),  # push fails
        ]
        assert push_local_commits() is False


def testpush_local_commits_skip_fetch():
    """push_local_commits skips fetch when skip_fetch=True."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0, stdout="0\n"),  # rev-list
        ]
        assert push_local_commits(skip_fetch=True) is True


def testpush_local_commits_exception():
    """push_local_commits returns False on unexpected exception."""
    with patch("subprocess.run", side_effect=OSError("disk error")):
        assert push_local_commits() is False


# ---------------------------------------------------------------------------
# Deploy rollback warning tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_warnings", "expected_warnings"),
    [
        (None, ["Deploy rolled back"]),
        ('["Earlier warning"]', ["Earlier warning", "Deploy rolled back"]),
        ("{invalid json}", ["Deploy rolled back"]),
    ],
)
async def test_failed_deploy_records_operator_boot_warning(
    tmp_path: Path,
    existing_warnings: str | None,
    expected_warnings: list[str],
):
    """POST /deploy preserves an actionable warning when rebase fails."""
    warnings_file = tmp_path / "boot_warnings.json"
    if existing_warnings is not None:
        warnings_file.write_text(existing_warnings)

    def failed_rebase(*args: str) -> subprocess.CompletedProcess[str]:
        if args == ("rebase", "origin/main"):
            return _cp(returncode=1, stderr="merge conflict")
        if args == ("stash",):
            return _cp(stdout="No local changes")
        return _cp()

    with (
        _patch_settings(data_dir=tmp_path),
        patch("pynchy.host.orchestrator.http_server.get_head_sha", return_value="same-sha"),
        patch("pynchy.host.orchestrator.http_server.push_local_commits", return_value=True),
        patch("pynchy.host.orchestrator.http_server.run_git", side_effect=failed_rebase),
        patch("pynchy.host.orchestrator.http_server.get_deploy_config_hash", return_value="config"),
        patch(
            "pynchy.host.orchestrator.http_server.start_deploy_workflow",
            new=AsyncMock(return_value=DeployClaim(DeployClaimStatus.CLAIMED)),
        ),
        patch(
            "pynchy.host.orchestrator.http_server.get_head_commit_message",
            return_value="commit",
        ),
        patch("pynchy.host.orchestrator.http_server.is_repo_dirty", return_value=False),
    ):
        runtime = ControlPlaneRuntime(
            bind_host="127.0.0.1",
            port=8484,
            unix_socket=None,
            public_bind=False,
            remote_auth_required=False,
            allow_remote_deploy=True,
            auth_token=None,
            rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        )
        app = create_http_app(MockHttpDeps(), runtime=runtime)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/deploy")
            assert response.status == 200
            assert (await response.json())["status"] == "restarting"
        finally:
            await client.close()

    warnings = json.loads(warnings_file.read_text())
    assert len(warnings) == len(expected_warnings)
    for actual, expected in zip(warnings, expected_warnings, strict=True):
        assert expected in actual


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class MockHttpDeps:
    """Mock implementation of HttpDeps for testing."""

    def __init__(self):
        self.messages_sent: list[tuple[str, str]] = []
        self.broadcasts: list[tuple[str, str]] = []
        self.user_messages: list[tuple[str, str]] = []
        self._groups = [{"jid": "test@g.us", "name": "Test Group"}]
        self._messages: list[NewMessage] = []
        self._connected = True
        self._admin_jid = "admin-1@g.us"
        self._event_callbacks: list = []
        self._periodic_agents: list[dict[str, Any]] = []

    async def send_message(self, jid: str, text: str) -> None:
        self.messages_sent.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.broadcasts.append((jid, text))

    def admin_chat_jid(self) -> str:
        return self._admin_jid

    def channels_connected(self) -> bool:
        return self._connected

    def get_groups(self) -> list[dict[str, Any]]:
        return self._groups

    async def get_messages(self, jid: str, limit: int) -> list[NewMessage]:
        return self._messages[-limit:]

    async def send_user_message(self, jid: str, content: str) -> None:
        self.user_messages.append((jid, content))

    def subscribe_events(self, callback) -> Any:
        self._event_callbacks.append(callback)
        return lambda: self._event_callbacks.remove(callback)

    async def get_periodic_agents(self) -> list[dict[str, Any]]:
        return self._periodic_agents

    def get_active_sessions(self) -> dict[str, str]:
        return {}

    def is_shutting_down(self) -> bool:
        return False


class TestHealthEndpoint(AioHTTPTestCase):
    """Tests for /health endpoint."""

    async def get_application(self) -> web.Application:
        self.deps = MockHttpDeps()
        return create_http_app(self.deps)

    async def test_health_returns_status_ok(self):
        """Health endpoint returns only its non-sensitive readiness contract."""
        resp = await self.client.get("/health")
        assert resp.status == 200
        assert await resp.json() == {"status": "ok"}

    async def test_health_does_not_inspect_repository_or_channel_state(self):
        """Public readiness cannot expose repository or channel details."""
        with (
            patch("pynchy.host.orchestrator.http_server.get_head_sha") as head_sha,
            patch("pynchy.host.orchestrator.http_server.get_head_commit_message") as head_commit,
            patch("pynchy.host.orchestrator.http_server.is_repo_dirty") as repo_dirty,
        ):
            resp = await self.client.get("/health")
        assert resp.status == 200
        head_sha.assert_not_called()
        head_commit.assert_not_called()
        repo_dirty.assert_not_called()


class TestTUIAPIEndpoints(AioHTTPTestCase):
    """Tests for TUI API endpoints."""

    async def get_application(self) -> web.Application:
        self.deps = MockHttpDeps()
        self.deps._messages = [
            NewMessage(
                id="m1",
                chat_jid="test@g.us",
                sender="user@s.whatsapp.net",
                sender_name="Alice",
                content="Hello",
                timestamp="2024-01-01T00:00:00.000Z",
                is_from_me=False,
            ),
            NewMessage(
                id="m2",
                chat_jid="test@g.us",
                sender="bot@s.whatsapp.net",
                sender_name="Bot",
                content="Hi Alice",
                timestamp="2024-01-01T00:00:01.000Z",
                is_from_me=True,
            ),
        ]
        self.deps._periodic_agents = [{"name": "test-agent", "status": "running"}]

        return create_http_app(self.deps)

    async def test_api_groups_returns_groups(self):
        """GET /api/groups returns registered groups."""
        resp = await self.client.get("/api/groups")
        assert resp.status == 200
        data = await resp.json()
        assert data == [{"jid": "test@g.us", "name": "Test Group"}]

    async def test_api_messages_returns_messages(self):
        """GET /api/messages returns chat history."""
        resp = await self.client.get("/api/messages?jid=test@g.us&limit=10")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 2
        assert data[0]["sender_name"] == "Alice"
        assert data[0]["content"] == "Hello"
        assert data[1]["sender_name"] == "Bot"
        assert data[1]["content"] == "Hi Alice"

    async def test_api_messages_requires_jid(self):
        """GET /api/messages requires jid parameter."""
        resp = await self.client.get("/api/messages")
        assert resp.status == 400
        data = await resp.json()
        assert "jid" in data["error"]

    async def test_api_messages_respects_limit(self):
        """GET /api/messages respects limit parameter."""
        resp = await self.client.get("/api/messages?jid=test@g.us&limit=1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1

    async def test_api_send_sends_message(self):
        """POST /api/send sends user message."""
        resp = await self.client.post(
            "/api/send",
            json={"jid": "test@g.us", "content": "Test message"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert self.deps.user_messages == [("test@g.us", "Test message")]

    async def test_api_send_requires_jid_and_content(self):
        """POST /api/send requires jid and content."""
        resp = await self.client.post("/api/send", json={"jid": "test@g.us"})
        assert resp.status == 400

        resp = await self.client.post("/api/send", json={"content": "test"})
        assert resp.status == 400

    async def test_api_periodic_returns_agents(self):
        """GET /api/periodic returns periodic agent status."""
        resp = await self.client.get("/api/periodic")
        assert resp.status == 200
        data = await resp.json()
        assert data == [{"name": "test-agent", "status": "running"}]
