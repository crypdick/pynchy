"""Tests for startup_handler startup helpers and plugin credential validation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.config.api import NotificationsConfig
from pynchy.deployments import (
    DeploymentState,
    DeployRevision,
)
from pynchy.host.orchestrator import startup_handler, startup_rollback
from pynchy.host.orchestrator.startup_handler import (
    InterruptedTurnRecovery,
    auto_rollback,
    check_deploy_continuation,
    claim_deploy_continuation,
    confirm_deploy_startup,
    finalize_deploy_startup,
    prepare_interrupted_turn_recovery,
    resolve_deploy_startup,
    send_boot_notification,
    terminate_failed_startup,
    validate_plugin_credentials,
)
from pynchy.identifiers import GroupFolder
from pynchy.state import (
    begin_in_flight_turn,
    claim_deployment,
    get_deployment_state,
    get_in_flight_turn,
    get_router_state,
    get_session,
    init_test_database,
    initialize_deployment_state,
    set_session,
)
from pynchy.workspace.api import WorkspaceProfile

_ACTIVE_REVISION = DeployRevision("active-sha", "active-config")


def _startup_without_deploy_continuation() -> InterruptedTurnRecovery:
    return InterruptedTurnRecovery(
        turns=(),
        commit_sha="unknown",
        resume_prompt="",
        had_deploy_continuation=False,
        deploy_revision=None,
        rolled_back=False,
        continuation_path=None,
    )


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

    @pytest.fixture(autouse=True)
    def _clean_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(startup_handler, "is_repo_dirty", lambda: False)
        monkeypatch.setattr(startup_handler, "get_head_sha", lambda: "prev-sha-1")

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
    async def test_notifies_admin_of_a_healthy_rollback_on_recovery(self, tmp_path, monkeypatch):
        """The next successful boot reports the failed and recovered SHAs to Discord."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "previous_commit_sha": "prev-sha-1",
                    "commit_sha": "failed-sha-2",
                    "resume_prompt": "Deploy complete.",
                }
            )
        )

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        with (
            patch(
                "pynchy.host.orchestrator.startup_handler.run_git", return_value=FakeResult()
            ) as mock_git,
            patch("pynchy.host.orchestrator.startup_handler.terminate_failed_startup") as terminate,
        ):
            await auto_rollback(cont_path, RuntimeError("startup failed"))

        assert [call.args for call in mock_git.call_args_list] == [
            ("status", "--porcelain", "--untracked-files=normal"),
            ("reset", "--hard", "prev-sha-1"),
        ]
        terminate.assert_called_once_with()

        updated = json.loads(cont_path.read_text())
        assert updated["resume_prompt"].startswith("ROLLBACK:")
        assert not updated["previous_commit_sha"]  # prevents loop
        assert updated["rolled_back"] is True

        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            lambda: make_settings(
                data_dir=tmp_path,
                notifications=NotificationsConfig(admin_workspace="admin"),
            ),
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_head_sha",
            lambda: "prev-sha-1",
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_head_commit_message",
            lambda _max_length: "Recovered deploy",
        )
        monkeypatch.setattr("pynchy.host.orchestrator.startup_handler.is_repo_dirty", lambda: False)
        deps = FakeDeps({"discord:admin": _make_workspace("discord:admin", "admin", True)})

        await send_boot_notification(deps)

        deps.broadcast_host_message.assert_awaited_once_with(
            "discord:admin",
            "🦞 online -- prev-sha Recovered deploy\n"
            "WARNING: Auto-deploy failed-sha-2 failed during startup: "
            "RuntimeError: startup failed. "
            "Rolled back to prev-sha-1. Server health: healthy (recovered after rollback).",
        )

    def test_failed_startup_uses_immediate_process_exit(self, monkeypatch) -> None:
        """Non-daemon plugin threads must not survive a rollback via SystemExit."""
        exit_codes: list[int] = []

        def record_exit(exit_code: int) -> None:
            exit_codes.append(exit_code)
            raise SystemExit(exit_code)

        monkeypatch.setattr(startup_rollback.os, "_exit", record_exit)

        with pytest.raises(SystemExit, match="1"):
            terminate_failed_startup()

        assert exit_codes == [1]

    @pytest.mark.asyncio
    async def test_fsyncs_rollback_evidence_before_immediate_exit(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """The hard exit follows durable continuation and warning renames."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "previous_commit_sha": "prev-sha-1",
                    "commit_sha": "failed-sha-2",
                }
            )
        )
        events: list[str] = []

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(startup_handler, "run_git", lambda *_args: FakeResult())
        monkeypatch.setattr(
            startup_rollback.os,
            "fsync",
            lambda _descriptor: events.append("fsync"),
        )
        monkeypatch.setattr(
            startup_handler,
            "terminate_failed_startup",
            lambda: events.append("terminate"),
        )

        await auto_rollback(cont_path, RuntimeError("startup failed"))

        assert events == ["fsync", "fsync", "fsync", "terminate"]

    @pytest.mark.asyncio
    async def test_replaces_malformed_boot_warnings_during_rollback(self, tmp_path, monkeypatch):
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "previous_commit_sha": "prev-sha-1",
                    "commit_sha": "failed-sha-2",
                }
            )
        )
        (tmp_path / "boot_warnings.json").write_text("not valid json")

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(startup_handler, "run_git", lambda *_args: FakeResult())
        monkeypatch.setattr(startup_handler, "terminate_failed_startup", lambda: None)

        await auto_rollback(cont_path, RuntimeError("startup failed"))

        warnings = json.loads((tmp_path / "boot_warnings.json").read_text())
        assert len(warnings) == 1
        assert "failed-sha-2" in warnings[0]
        assert "prev-sha-1" in warnings[0]

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
        self.sessions: dict[str, str] = {}
        self.session_cleared: set[str] = set()
        self.broadcast_host_message = AsyncMock()
        self.start_interactive_turn = AsyncMock()
        self.start_interrupted_turn = AsyncMock()
        self.register_workspace = AsyncMock()
        self.prepare_context_reset = AsyncMock()
        self.destroy_runtime_session = AsyncMock()
        self.has_api_credentials = MagicMock(return_value=True)

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
        control_state: CheckpointControlState = CheckpointControlState.ACTIVE,
        claimed_at: str | None = "2026-07-14T10:00:01+00:00",
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
            claimed_at=claimed_at,
            control_state=control_state,
        )

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

        resumed_chats = await check_deploy_continuation(deps, active_revision=_ACTIVE_REVISION)

        assert deps.broadcast_host_message.await_count == 2
        notified_jids = {call.args[0] for call in deps.broadcast_host_message.await_args_list}
        assert notified_jids == {periodic_jid, interactive_jid}
        assert {call.args for call in deps.start_interrupted_turn.await_args_list} == {
            ("turn-scheduled", "code-improver"),
            ("turn-interactive", "my-group"),
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

        resumed_chats = await check_deploy_continuation(deps, active_revision=_ACTIVE_REVISION)

        assert resumed_chats == set()
        deps.broadcast_host_message.assert_not_awaited()
        deps.start_interrupted_turn.assert_not_awaited()
        deps.start_interactive_turn.assert_not_awaited()
        assert deps.queue.enqueued == []

    @pytest.mark.asyncio
    async def test_recovers_after_crash_without_continuation_file(self, tmp_path, monkeypatch):
        """The DB ledger is authoritative even when no deploy file was written."""
        await init_test_database()
        previous_jid = "slack:INTERRUPTED"
        current_jid = "slack:CURRENT"
        deps = FakeDeps({current_jid: _make_workspace(current_jid, "my-group")})
        await begin_in_flight_turn(
            self._turn(
                "turn-crash",
                previous_jid,
                "my-group",
                InFlightWorkKind.INTERACTIVE,
            )
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

        resumed_chats = await check_deploy_continuation(deps, active_revision=_ACTIVE_REVISION)

        assert resumed_chats == {current_jid}
        deps.start_interrupted_turn.assert_awaited_once_with("turn-crash", "my-group")
        deps.broadcast_host_message.assert_awaited_once()
        assert deps.broadcast_host_message.await_args.args[0] == current_jid
        notice = deps.broadcast_host_message.await_args.args[1]
        assert "Pynchy restarted" in notice
        assert "Deploy complete" not in notice

    @pytest.mark.asyncio
    async def test_startup_finishes_pause_transition_without_dispatching_it(
        self, tmp_path, monkeypatch
    ):
        await init_test_database()
        jid = "slack:PAUSED"
        deps = FakeDeps({jid: _make_workspace(jid, "paused-group")})
        await begin_in_flight_turn(
            self._turn(
                "turn-pausing",
                jid,
                "paused-group",
                InFlightWorkKind.INTERACTIVE,
                control_state=CheckpointControlState.PAUSE_REQUESTED,
            )
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

        assert await check_deploy_continuation(deps, active_revision=_ACTIVE_REVISION) == set()

        paused = await get_in_flight_turn("turn-pausing")
        assert paused is not None
        assert paused.control_state is CheckpointControlState.PAUSED
        assert paused.claimed_at is None
        deps.start_interrupted_turn.assert_not_awaited()
        deps.broadcast_host_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_startup_completes_reset_and_discards_provider_session(
        self, tmp_path, monkeypatch
    ):
        await init_test_database()
        jid = "slack:RESETTING"
        folder = GroupFolder("reset-group")
        deps = FakeDeps({jid: _make_workspace(jid, str(folder))})
        deps.sessions[str(folder)] = "provider-thread"
        await set_session(folder, "provider-thread")
        await begin_in_flight_turn(
            self._turn(
                "turn-resetting",
                jid,
                str(folder),
                InFlightWorkKind.SCHEDULED,
                task_id="recurring-task",
                control_state=CheckpointControlState.RESET_REQUESTED,
            )
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

        assert await check_deploy_continuation(deps, active_revision=_ACTIVE_REVISION) == set()

        assert await get_in_flight_turn("turn-resetting") is None
        assert await get_session(folder) is None
        assert str(folder) not in deps.sessions
        assert str(folder) in deps.session_cleared
        deps.prepare_context_reset.assert_awaited_once_with(deps.workspaces[jid])
        deps.start_interrupted_turn.assert_not_awaited()


class TestConfirmDeployStartup:
    @pytest.mark.asyncio
    async def test_promotes_a_healthy_external_release(self) -> None:
        await init_test_database()
        applied = DeployRevision("old-sha", "config-a")
        active = DeployRevision("new-sha", "config-b")
        await initialize_deployment_state(applied)

        await resolve_deploy_startup(
            _startup_without_deploy_continuation(),
            active_revision=active,
        )

        assert await get_deployment_state() == DeploymentState(applied=active, pending=None)
        assert await get_router_state("last_deploy_sha") == active.commit_sha
        assert await get_router_state("last_deploy_at") is not None

    @pytest.mark.asyncio
    async def test_does_not_rewrite_metadata_for_same_external_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await init_test_database()
        active = DeployRevision("active-sha", "config-a")
        recovery = _startup_without_deploy_continuation()
        await initialize_deployment_state(active)
        await resolve_deploy_startup(recovery, active_revision=active)
        complete = AsyncMock()
        monkeypatch.setattr(startup_handler, "complete_deployment", complete)

        await resolve_deploy_startup(recovery, active_revision=active)

        complete.assert_not_awaited()

    """Tests for promoting a claimed revision only after a successful boot."""

    @staticmethod
    def _settings(monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "pynchy.host.orchestrator.startup_handler.get_settings",
            type("S", (), {"data_dir": tmp_path}),
        )

    @pytest.mark.asyncio
    async def test_finalizer_does_not_delete_a_new_continuation_generation(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        await init_test_database()
        original = tmp_path / "deploy_continuation.json"
        original.write_text(json.dumps({"commit_sha": "old-sha"}), encoding="utf-8")
        self._settings(monkeypatch, tmp_path)

        recovery = await prepare_interrupted_turn_recovery(
            continuation_path=claim_deploy_continuation(tmp_path)
        )
        original.write_text(json.dumps({"commit_sha": "new-sha"}), encoding="utf-8")
        await finalize_deploy_startup(recovery)

        assert json.loads(original.read_text(encoding="utf-8")) == {"commit_sha": "new-sha"}
        assert not (tmp_path / "deploy_continuation.startup.json").exists()

    def test_claim_prefers_a_new_canonical_generation(self, tmp_path) -> None:
        claimed = tmp_path / "deploy_continuation.startup.json"
        canonical = tmp_path / "deploy_continuation.json"
        claimed.write_text(json.dumps({"commit_sha": "old-sha"}), encoding="utf-8")
        canonical.write_text(json.dumps({"commit_sha": "new-sha"}), encoding="utf-8")

        result = claim_deploy_continuation(tmp_path)

        assert result == claimed
        assert json.loads(claimed.read_text(encoding="utf-8")) == {"commit_sha": "new-sha"}
        assert not canonical.exists()

    @pytest.mark.asyncio
    async def test_promotes_the_continuation_revision_before_sync_can_poll(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        await init_test_database()
        applied = DeployRevision("old-sha", "config-a")
        target = DeployRevision("new-sha", "config-b")
        await initialize_deployment_state(applied)
        await claim_deployment(target)
        continuation_path = tmp_path / "deploy_continuation.json"
        continuation_path.write_text(
            json.dumps(
                {
                    "commit_sha": target.commit_sha,
                    "config_hash": target.config_hash,
                    "resume_prompt": "Deploy complete.",
                }
            )
        )
        self._settings(monkeypatch, tmp_path)

        recovery = await prepare_interrupted_turn_recovery(
            continuation_path=claim_deploy_continuation(tmp_path)
        )
        claimed_path = tmp_path / "deploy_continuation.startup.json"
        assert claimed_path.exists()
        assert not continuation_path.exists()
        await confirm_deploy_startup(recovery, active_revision=target)

        assert not claimed_path.exists()
        assert recovery.deploy_revision == target
        assert await get_deployment_state() == DeploymentState(
            applied=target,
            pending=None,
        )
        assert await get_router_state("last_deploy_sha") == target.commit_sha
        assert await get_router_state("last_deploy_at") is not None

    @pytest.mark.asyncio
    async def test_rollback_releases_the_failed_revision_without_promoting_it(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        await init_test_database()
        applied = DeployRevision("old-sha", "config-a")
        failed = DeployRevision("failed-sha", "config-b")
        await initialize_deployment_state(applied)
        await claim_deployment(failed)
        continuation_path = tmp_path / "deploy_continuation.json"
        continuation_path.write_text(
            json.dumps(
                {
                    "commit_sha": failed.commit_sha,
                    "config_hash": failed.config_hash,
                    "rolled_back": True,
                }
            )
        )
        self._settings(monkeypatch, tmp_path)

        recovery = await prepare_interrupted_turn_recovery(
            continuation_path=claim_deploy_continuation(tmp_path)
        )
        claimed_path = tmp_path / "deploy_continuation.startup.json"
        assert claimed_path.exists()
        assert not continuation_path.exists()
        await confirm_deploy_startup(recovery, active_revision=applied)

        assert not claimed_path.exists()
        state = await get_deployment_state()
        assert state.applied == applied
        assert state.pending is None
