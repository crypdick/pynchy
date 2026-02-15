"""Tests for HTTP server endpoints and utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from pynchy.git_utils import get_head_sha, is_repo_dirty, push_local_commits
from pynchy.http_server import (
    _get_head_commit_message,
    _write_boot_warning,
    deps_key,
)
from pynchy.types import NewMessage

# ---------------------------------------------------------------------------
# Git utility tests
# ---------------------------------------------------------------------------


def test_get_head_sha_success():
    """get_head_sha returns SHA when git succeeds."""
    with patch("pynchy.git_utils.run_git") as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout="abc123def456\n",
        )
        assert get_head_sha() == "abc123def456"


def test_get_head_sha_failure():
    """get_head_sha returns 'unknown' when git fails."""
    with patch("pynchy.git_utils.run_git") as mock_run:
        mock_run.return_value = Mock(returncode=1, stdout="")
        assert get_head_sha() == "unknown"


def test_is_repo_dirty_clean():
    """is_repo_dirty returns False when no uncommitted changes."""
    with patch("pynchy.git_utils.run_git") as mock_run:
        mock_run.return_value = Mock(returncode=0, stdout="")
        assert is_repo_dirty() is False


def test_is_repo_dirty_has_changes():
    """is_repo_dirty returns True when uncommitted changes exist."""
    with patch("pynchy.git_utils.run_git") as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout=" M src/pynchy/app.py\n?? newfile.txt\n",
        )
        assert is_repo_dirty() is True


def test_is_repo_dirty_failure():
    """is_repo_dirty returns False when git fails."""
    with patch("pynchy.git_utils.run_git") as mock_run:
        mock_run.return_value = Mock(returncode=1, stdout="")
        assert is_repo_dirty() is False


def test_get_head_commit_message_success():
    """_get_head_commit_message returns commit subject."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(
            returncode=0,
            stdout="Add feature X\n",
        )
        assert _get_head_commit_message() == "Add feature X"


def test_get_head_commit_message_truncation():
    """_get_head_commit_message truncates long subjects."""
    long_msg = "A" * 80
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(returncode=0, stdout=f"{long_msg}\n")
        result = _get_head_commit_message(max_length=72)
        assert len(result) == 72
        assert result.endswith("…")


def test_get_head_commit_message_failure():
    """_get_head_commit_message returns empty string on failure."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(returncode=1, stdout="")
        assert _get_head_commit_message() == ""


def test_get_head_commit_message_exception():
    """_get_head_commit_message returns empty string when subprocess raises."""
    with patch("subprocess.run", side_effect=OSError):
        assert _get_head_commit_message() == ""


# ---------------------------------------------------------------------------
# Push local commits tests
# ---------------------------------------------------------------------------


def testpush_local_commits_nothing_to_push():
    """push_local_commits returns True when no local commits exist."""
    with patch("subprocess.run") as mock_run:
        # fetch succeeds, rev-list shows 0 commits
        mock_run.side_effect = [
            Mock(returncode=0),  # fetch
            Mock(returncode=0, stdout="0\n"),  # rev-list
        ]
        assert push_local_commits() is True


def testpush_local_commits_success():
    """push_local_commits returns True when push succeeds."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            Mock(returncode=0),  # fetch
            Mock(returncode=0, stdout="3\n"),  # rev-list (3 commits)
            Mock(returncode=0),  # rebase
            Mock(returncode=0),  # push
        ]
        assert push_local_commits() is True


def testpush_local_commits_fetch_failure():
    """push_local_commits returns False when fetch fails."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(returncode=1, stderr="network error")
        assert push_local_commits() is False


def testpush_local_commits_rebase_failure_retries_and_fails():
    """push_local_commits retries once after rebase failure, then gives up."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            Mock(returncode=0),  # fetch
            Mock(returncode=0, stdout="2\n"),  # rev-list
            Mock(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 1)
            Mock(returncode=0),  # rebase --abort
            Mock(returncode=0),  # retry fetch
            Mock(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 2)
            Mock(returncode=0),  # rebase --abort
        ]
        assert push_local_commits() is False


def testpush_local_commits_rebase_retry_succeeds():
    """push_local_commits succeeds on retry when origin advanced mid-push."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            Mock(returncode=0),  # fetch
            Mock(returncode=0, stdout="2\n"),  # rev-list
            Mock(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 1)
            Mock(returncode=0),  # rebase --abort
            Mock(returncode=0),  # retry fetch
            Mock(returncode=0),  # rebase succeeds (attempt 2)
            Mock(returncode=0),  # push
        ]
        assert push_local_commits() is True


def testpush_local_commits_push_failure():
    """push_local_commits returns False when push is rejected."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            Mock(returncode=0),  # fetch
            Mock(returncode=0, stdout="1\n"),  # rev-list
            Mock(returncode=0),  # rebase
            Mock(returncode=1, stderr="rejected"),  # push fails
        ]
        assert push_local_commits() is False


def testpush_local_commits_skip_fetch():
    """push_local_commits skips fetch when skip_fetch=True."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            Mock(returncode=0, stdout="0\n"),  # rev-list
        ]
        assert push_local_commits(skip_fetch=True) is True


def testpush_local_commits_exception():
    """push_local_commits returns False on unexpected exception."""
    with patch("subprocess.run", side_effect=OSError("disk error")):
        assert push_local_commits() is False


# ---------------------------------------------------------------------------
# Boot warning tests
# ---------------------------------------------------------------------------


def test_write_boot_warning_creates_file(tmp_path: Path):
    """_write_boot_warning creates boot_warnings.json with message."""
    with patch("pynchy.http_server.DATA_DIR", tmp_path):
        _write_boot_warning("Test warning")
        warnings_file = tmp_path / "boot_warnings.json"
        assert warnings_file.exists()
        warnings = json.loads(warnings_file.read_text())
        assert warnings == ["Test warning"]


def test_write_boot_warning_appends_to_existing(tmp_path: Path):
    """_write_boot_warning appends to existing warnings."""
    with patch("pynchy.http_server.DATA_DIR", tmp_path):
        # First warning
        _write_boot_warning("Warning 1")
        # Second warning
        _write_boot_warning("Warning 2")

        warnings_file = tmp_path / "boot_warnings.json"
        warnings = json.loads(warnings_file.read_text())
        assert warnings == ["Warning 1", "Warning 2"]


def test_write_boot_warning_handles_corrupted_file(tmp_path: Path):
    """_write_boot_warning creates new array if file is corrupted."""
    with patch("pynchy.http_server.DATA_DIR", tmp_path):
        warnings_file = tmp_path / "boot_warnings.json"
        warnings_file.write_text("{invalid json}")

        _write_boot_warning("New warning")

        warnings = json.loads(warnings_file.read_text())
        assert warnings == ["New warning"]


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
        self._god_jid = "god@g.us"
        self._event_callbacks: list = []
        self._periodic_agents: list[dict[str, Any]] = []

    async def send_message(self, jid: str, text: str) -> None:
        self.messages_sent.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.broadcasts.append((jid, text))

    def god_chat_jid(self) -> str:
        return self._god_jid

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


class TestHealthEndpoint(AioHTTPTestCase):
    """Tests for /health endpoint."""

    async def get_application(self) -> web.Application:
        from pynchy.http_server import _handle_health

        app = web.Application()
        self.deps = MockHttpDeps()
        app[deps_key] = self.deps
        app.router.add_get("/health", _handle_health)
        return app

    async def test_health_returns_status_ok(self):
        """Health endpoint returns ok status."""
        with patch("pynchy.http_server.get_head_sha", return_value="abc123"):
            with patch("pynchy.http_server._get_head_commit_message", return_value="Test commit"):
                with patch("pynchy.http_server.is_repo_dirty", return_value=False):
                    resp = await self.client.get("/health")
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "ok"
                    assert data["head_sha"] == "abc123"
                    assert data["head_commit"] == "Test commit"
                    assert data["dirty"] is False
                    assert data["channels_connected"] is True
                    assert "uptime_seconds" in data

    async def test_health_includes_uptime(self):
        """Health endpoint includes uptime_seconds."""
        with patch("pynchy.http_server.get_head_sha", return_value="abc123"):
            with patch("pynchy.http_server._get_head_commit_message", return_value="Test"):
                with patch("pynchy.http_server.is_repo_dirty", return_value=False):
                    resp = await self.client.get("/health")
                    data = await resp.json()
                    assert isinstance(data["uptime_seconds"], int)
                    assert data["uptime_seconds"] >= 0


class TestTUIAPIEndpoints(AioHTTPTestCase):
    """Tests for TUI API endpoints."""

    async def get_application(self) -> web.Application:
        from pynchy.http_server import (
            _handle_api_groups,
            _handle_api_messages,
            _handle_api_periodic,
            _handle_api_send,
        )

        app = web.Application()
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

        app[deps_key] = self.deps
        app.router.add_get("/api/groups", _handle_api_groups)
        app.router.add_get("/api/messages", _handle_api_messages)
        app.router.add_post("/api/send", _handle_api_send)
        app.router.add_get("/api/periodic", _handle_api_periodic)
        return app

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
