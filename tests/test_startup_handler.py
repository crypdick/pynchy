"""Tests for startup_handler startup helpers and plugin credential validation."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.orchestrator.startup_handler import (
    auto_rollback,
    check_deploy_continuation,
    validate_plugin_credentials,
)
from pynchy.state import begin_in_flight_turn, init_test_database
from pynchy.types import InFlightTurn, InFlightWorkKind, WorkspaceProfile

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
            patch(
                "pynchy.host.orchestrator.startup_handler.run_git", return_value=FakeResult()
            ) as mock_git,
            pytest.raises(SystemExit) as exc_info,
        ):
            await auto_rollback(cont_path, RuntimeError("startup failed"))

        mock_git.assert_called_once_with("reset", "--hard", "prev-sha-1")
        assert exc_info.value.code == 1

        # Continuation should be rewritten with rollback info
        updated = json.loads(cont_path.read_text())
        assert "ROLLBACK" in updated["resume_prompt"]
        assert not updated["previous_commit_sha"]  # prevents loop

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
        self.start_interactive_turn = AsyncMock()
        self.start_interrupted_turn = AsyncMock()
        self.register_workspace = AsyncMock()

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._workspaces


class TestCheckDeployContinuation:
    """Tests for durable interrupted-turn dispatch on startup."""

    @staticmethod
    def _turn(
        turn_id: str,
        chat_jid: str,
        folder: str,
        work_kind: InFlightWorkKind,
        *,
        task_id: str | None = None,
    ) -> InFlightTurn:
        return InFlightTurn(
            turn_id=turn_id,
            chat_jid=chat_jid,
            group_folder=folder,
            work_kind=work_kind,
            input_messages=[{"content": "finish this"}],
            input_start_cursor="before",
            input_end_cursor="after",
            started_at="2026-07-14T10:00:00+00:00",
            task_id=task_id,
            claimed_at="2026-07-14T10:00:01+00:00",
        )

    @pytest.mark.asyncio
    async def test_prunes_migration_backups_after_successful_deploy(self, tmp_path, monkeypatch):
        """Deploy continuation consumption should bound migration backup growth."""
        await init_test_database()
        cont_path = tmp_path / "deploy_continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "commit_sha": "abc123",
                    "resume_prompt": "Deploy complete.",
                    "interrupted_turns": [],
                }
            )
        )
        backups = tmp_path / "migration-backups"
        backups.mkdir()
        oldest = backups / "20260704-runtime"
        old = backups / "20260705-runtime"
        mid = backups / "20260706-runtime"
        new = backups / "20260707-runtime"
        for index, path in enumerate((oldest, old, mid, new), start=1):
            path.mkdir()
            os.utime(path, (index, index))

        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

        await check_deploy_continuation(FakeDeps({}))

        assert not oldest.exists()
        assert old.exists()
        assert mid.exists()
        assert new.exists()

    @pytest.mark.asyncio
    async def test_dispatches_each_durable_interrupted_turn(self, tmp_path, monkeypatch):
        """Interactive and scheduled rows get dedicated recovery workflows."""
        await init_test_database()
        periodic_jid = "slack:PERIODIC"
        interactive_jid = "slack:INTERACTIVE"

        ws = {
            periodic_jid: _make_workspace(periodic_jid, "code-improver"),
            interactive_jid: _make_workspace(interactive_jid, "my-group"),
        }
        deps = FakeDeps(ws)

        await begin_in_flight_turn(
            self._turn(
                "turn-scheduled",
                periodic_jid,
                "code-improver",
                InFlightWorkKind.SCHEDULED,
                task_id="task-1",
            )
        )
        await begin_in_flight_turn(
            self._turn(
                "turn-interactive",
                interactive_jid,
                "my-group",
                InFlightWorkKind.INTERACTIVE,
            )
        )

        # Write continuation file
        cont_path = tmp_path / "deploy_continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "commit_sha": "abc123",
                    "resume_prompt": "Deploy complete.",
                    "interrupted_turns": ["diagnostic-only"],
                }
            )
        )

        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_head_commit_message",
            lambda *a: "test commit",
        )

        resumed_chats = await check_deploy_continuation(deps)

        assert deps.broadcast_host_message.await_count == 2
        notified_jids = {call.args[0] for call in deps.broadcast_host_message.await_args_list}
        assert notified_jids == {periodic_jid, interactive_jid}
        assert {call.args[0] for call in deps.start_interrupted_turn.await_args_list} == {
            "turn-scheduled",
            "turn-interactive",
        }
        assert resumed_chats == {periodic_jid, interactive_jid}
        deps.start_interactive_turn.assert_not_awaited()
        assert deps.queue.enqueued == []

    @pytest.mark.asyncio
    async def test_saved_sessions_do_not_wake_idle_conversations(self, tmp_path, monkeypatch):
        """Legacy session metadata is not evidence that agent work was running."""
        await init_test_database()
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
            type("S", (), {"data_dir": tmp_path}),
        )

        resumed_chats = await check_deploy_continuation(deps)

        assert resumed_chats == set()
        deps.broadcast_host_message.assert_not_awaited()
        deps.start_interrupted_turn.assert_not_awaited()
        deps.start_interactive_turn.assert_not_awaited()
        assert deps.queue.enqueued == []

    @pytest.mark.asyncio
    async def test_recovers_after_crash_without_continuation_file(self, tmp_path, monkeypatch):
        """The DB ledger is authoritative even when no deploy file was written."""
        await init_test_database()
        jid = "slack:INTERRUPTED"
        deps = FakeDeps({jid: _make_workspace(jid, "my-group")})
        await begin_in_flight_turn(
            self._turn(
                "turn-crash",
                jid,
                "my-group",
                InFlightWorkKind.INTERACTIVE,
            )
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

        resumed_chats = await check_deploy_continuation(deps)

        assert resumed_chats == {jid}
        deps.start_interrupted_turn.assert_awaited_once_with("turn-crash")
        notice = deps.broadcast_host_message.await_args.args[1]
        assert "Pynchy restarted" in notice
        assert "Deploy complete" not in notice
