"""Tests for pynchy.host.orchestrator.startup_handler — startup helpers and plugin credential validation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.orchestrator.startup_handler import (
    auto_rollback,
    check_deploy_continuation,
    validate_plugin_credentials,
)
from pynchy.types import WorkspaceProfile

# ---------------------------------------------------------------------------
# validate_plugin_credentials
# ---------------------------------------------------------------------------


class TestValidatePluginCredentials:
    """Tests for checking plugin environment variable requirements."""

    def test_returns_empty_when_no_requires_credentials(self):
        """Plugins without requires_credentials() need no credentials."""

        class NoCredsPlugin:
            pass

        assert validate_plugin_credentials(NoCredsPlugin()) == []

    def test_returns_empty_when_all_present(self, monkeypatch):
        """All required credentials are in the environment."""

        class Plugin:
            def requires_credentials(self):
                return ["MY_API_KEY", "MY_SECRET"]

        monkeypatch.setenv("MY_API_KEY", "key-123")
        monkeypatch.setenv("MY_SECRET", "secret-456")
        assert validate_plugin_credentials(Plugin()) == []

    def test_returns_missing_credentials(self, monkeypatch):
        """Missing credentials are returned in the list."""

        class Plugin:
            def requires_credentials(self):
                return ["PRESENT_KEY", "MISSING_KEY"]

        monkeypatch.setenv("PRESENT_KEY", "value")
        monkeypatch.delenv("MISSING_KEY", raising=False)
        result = validate_plugin_credentials(Plugin())
        assert result == ["MISSING_KEY"]

    def test_returns_all_missing_when_none_present(self, monkeypatch):
        """All credentials missing when none are in the environment."""

        class Plugin:
            def requires_credentials(self):
                return ["KEY_A", "KEY_B"]

        monkeypatch.delenv("KEY_A", raising=False)
        monkeypatch.delenv("KEY_B", raising=False)
        result = validate_plugin_credentials(Plugin())
        assert set(result) == {"KEY_A", "KEY_B"}

    def test_empty_requires_list(self):
        """Plugin requires no credentials (empty list)."""

        class Plugin:
            def requires_credentials(self):
                return []

        assert validate_plugin_credentials(Plugin()) == []


# ---------------------------------------------------------------------------
# auto_rollback
# ---------------------------------------------------------------------------


class TestAutoRollback:
    """Tests for auto_rollback — rolls back to previous commit on startup failure."""

    @pytest.mark.asyncio
    async def test_skips_when_file_unreadable(self, tmp_path):
        """Should return early when continuation file can't be read."""
        bad_path = tmp_path / "continuation.json"
        bad_path.write_text("not valid json")

        await auto_rollback(bad_path, RuntimeError("startup failed"))
        # Should not raise — just logs and returns

    @pytest.mark.asyncio
    async def test_skips_when_no_previous_sha(self, tmp_path):
        """Should return early when previous_commit_sha is empty."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(json.dumps({"previous_commit_sha": ""}))

        await auto_rollback(cont_path, RuntimeError("startup failed"))
        # Should not raise — just logs and returns

    @pytest.mark.asyncio
    async def test_performs_rollback_and_rewrites_continuation(self, tmp_path):
        """Should git reset to previous SHA and rewrite continuation."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "previous_commit_sha": "prev-sha-1",
                    "resume_prompt": "Deploy complete.",
                }
            )
        )

        class FakeResult:
            returncode = 0
            stderr = ""

        with (
            patch("pynchy.host.orchestrator.startup_handler.run_git", return_value=FakeResult()) as mock_git,
            pytest.raises(SystemExit) as exc_info,
        ):
            await auto_rollback(cont_path, RuntimeError("startup failed"))

        mock_git.assert_called_once_with("reset", "--hard", "prev-sha-1")
        assert exc_info.value.code == 1

        # Continuation should be rewritten with rollback info
        updated = json.loads(cont_path.read_text())
        assert "ROLLBACK" in updated["resume_prompt"]
        assert updated["previous_commit_sha"] == ""  # prevents loop

    @pytest.mark.asyncio
    async def test_returns_when_git_reset_fails(self, tmp_path):
        """Should return (not exit) when git reset fails."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(json.dumps({"previous_commit_sha": "prev-sha-1"}))

        class FailResult:
            returncode = 1
            stderr = "fatal: not a git repo"

        with patch("pynchy.host.orchestrator.startup_handler.run_git", return_value=FailResult()):
            await auto_rollback(cont_path, RuntimeError("startup failed"))
        # Should not raise — git reset failure is logged and returned

    @pytest.mark.asyncio
    async def test_skips_when_file_does_not_exist(self, tmp_path):
        """Should return early when continuation file doesn't exist."""
        missing_path = tmp_path / "no_such_file.json"

        await auto_rollback(missing_path, RuntimeError("startup failed"))
        # Should not raise


# ---------------------------------------------------------------------------
# check_deploy_continuation
# ---------------------------------------------------------------------------


def _make_workspace(jid: str, folder: str, is_admin: bool = False) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=jid,
        name=folder,
        folder=folder,
        trigger="always",
        is_admin=is_admin,
    )


class FakeQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    def enqueue_message_check(self, jid: str) -> None:
        self.enqueued.append(jid)


class FakeDeps:
    def __init__(self, ws: dict[str, WorkspaceProfile]):
        self._workspaces = ws
        self.queue = FakeQueue()
        self.last_agent_timestamp: dict[str, str] = {}
        self.channels: list = []
        self.broadcast_host_message = AsyncMock()
        self.broadcast_system_notice = AsyncMock()
        self._register_workspace = AsyncMock()

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._workspaces


class TestCheckDeployContinuation:
    """Tests for check_deploy_continuation — inject resume messages on deploy."""

    @pytest.mark.asyncio
    async def test_skips_periodic_workspace(self, tmp_path, monkeypatch):
        """Periodic workspaces should NOT receive deploy resume messages."""
        periodic_jid = "slack:PERIODIC"
        interactive_jid = "slack:INTERACTIVE"

        ws = {
            periodic_jid: _make_workspace(periodic_jid, "code-improver"),
            interactive_jid: _make_workspace(interactive_jid, "my-group"),
        }
        deps = FakeDeps(ws)

        # Write continuation file
        cont_path = tmp_path / "deploy_continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "commit_sha": "abc123",
                    "resume_prompt": "Deploy complete.",
                    "active_sessions": {
                        periodic_jid: "session-1",
                        interactive_jid: "session-2",
                    },
                }
            )
        )

        # Patch settings to point data_dir at tmp_path
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            lambda: type("S", (), {"data_dir": tmp_path})(),
        )

        # Patch load_workspace_config: periodic for code-improver, non-periodic for others
        from pynchy.config.models import WorkspaceConfig

        def mock_load(folder):
            if folder == "code-improver":
                return WorkspaceConfig(schedule="0 */1 * * *", prompt="Run task.")
            return WorkspaceConfig()

        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.load_workspace_config",
            mock_load,
        )
        # Stub get_head_commit_message so it doesn't touch the real repo
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_head_commit_message",
            lambda *a: "test commit",
        )

        await check_deploy_continuation(deps)

        # Only the interactive workspace should get a resume notice
        deps.broadcast_system_notice.assert_awaited_once()
        call_jid = deps.broadcast_system_notice.call_args[0][0]
        assert call_jid == interactive_jid
        assert len(deps.queue.enqueued) == 1
        assert deps.queue.enqueued[0] == interactive_jid

    @pytest.mark.asyncio
    async def test_resumes_interactive_workspace(self, tmp_path, monkeypatch):
        """Non-periodic workspaces should receive deploy resume messages."""
        jid = "slack:INTERACTIVE"
        ws = {jid: _make_workspace(jid, "my-group")}
        deps = FakeDeps(ws)

        cont_path = tmp_path / "deploy_continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "commit_sha": "abc123",
                    "resume_prompt": "Deploy complete.",
                    "active_sessions": {jid: "session-1"},
                }
            )
        )

        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            lambda: type("S", (), {"data_dir": tmp_path})(),
        )

        from pynchy.config.models import WorkspaceConfig

        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.load_workspace_config",
            lambda folder: WorkspaceConfig(),
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_head_commit_message",
            lambda *a: "test commit",
        )

        await check_deploy_continuation(deps)

        deps.broadcast_system_notice.assert_awaited_once()
        call_jid, call_text = deps.broadcast_system_notice.call_args[0]
        assert call_jid == jid
        assert "Deploy complete" in call_text
