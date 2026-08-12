"""Tests for deploy logic.

Tests finalize_deploy() which coordinates continuation file writing,
user notification, and process restart via SIGTERM. Errors here could
leave the service in a broken state or lose deploy context.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # noqa: S404 - tests construct completed process results without executing commands.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.deployments import DeployChangeKind
from pynchy.host.orchestrator.deploy import (
    BuildResult,
    DeployGitRuntime,
    RollbackResult,
    build_container_image,
    configure_deploy_git_runtime,
    current_deploy_revision,
    finalize_deploy,
    rollback_deploy_checkout,
)

_CONFIG_HASH = "config-hash-001"


def test_current_deploy_revision_requires_configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.host.orchestrator.deploy._runtime.runtime",
        None,
    )

    with pytest.raises(RuntimeError, match="Deploy Git runtime has not been configured"):
        current_deploy_revision()


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def deploy_dir(tmp_path: Path):
    """Provide an isolated continuation directory for deploy tests."""
    with patch(
        "pynchy.state.api.get_in_flight_turns",
        new_callable=AsyncMock,
        return_value=[],
    ):
        yield tmp_path


class TestFinalizeDeploy:
    """Test the finalize_deploy() function which orchestrates service restarts."""

    async def test_writes_continuation_file(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="commit-sha-001",
                config_hash=_CONFIG_HASH,
                previous_sha="previous-sha-001",
                change_kind=DeployChangeKind.CODE,
                data_dir=deploy_dir,
                resume_prompt="Deploy complete.",
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert continuation["chat_jid"] == "group@g.us"
        assert continuation["commit_sha"] == "commit-sha-001"
        assert continuation["config_hash"] == _CONFIG_HASH
        assert continuation["change_kind"] == "code change"
        assert continuation["previous_commit_sha"] == "previous-sha-001"
        assert continuation["resume_prompt"] == "Deploy complete."
        assert continuation["interrupted_turns"] == []
        assert "active_sessions" not in continuation


class TestRollbackDeployCheckout:
    """Tests for restoring a checkout when deploy preparation fails."""

    def test_returns_the_verified_sha_after_reset(self):
        reset_result = subprocess.CompletedProcess(
            args=["git", "reset", "--hard", "previous-sha"],
            returncode=0,
            stdout="",
            stderr="",
        )
        run_git = MagicMock(return_value=reset_result)
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "previous-sha-full",
                get_deploy_config_hash=lambda: _CONFIG_HASH,
                run_git=run_git,
            )
        )
        result = rollback_deploy_checkout("previous-sha")

        assert [call.args for call in run_git.call_args_list] == [
            ("status", "--porcelain", "--untracked-files=normal"),
            ("reset", "--hard", "previous-sha"),
        ]
        assert result.success is True
        assert result.actual_sha == "previous-sha-full"

    def test_preserves_dirty_work_around_reset(self):
        run_git = MagicMock(
            side_effect=[
                subprocess.CompletedProcess([], 0, " M operator.txt", ""),
                subprocess.CompletedProcess([], 0, "Saved working directory", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
        )
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "previous-sha",
                get_deploy_config_hash=lambda: _CONFIG_HASH,
                run_git=run_git,
            )
        )

        result = rollback_deploy_checkout("previous-sha")

        assert result.success is True
        assert [call.args for call in run_git.call_args_list] == [
            ("status", "--porcelain", "--untracked-files=normal"),
            ("stash", "push", "--include-untracked"),
            ("reset", "--hard", "previous-sha"),
            ("stash", "pop"),
        ]

    def test_fails_closed_when_dirty_status_is_unknown(self):
        run_git = MagicMock(
            return_value=subprocess.CompletedProcess([], 1, "", "fatal: status unavailable")
        )
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "unused",
                get_deploy_config_hash=lambda: _CONFIG_HASH,
                run_git=run_git,
            )
        )

        result = rollback_deploy_checkout("previous-sha")

        assert result.success is False
        assert result.error == "git status failed: fatal: status unavailable"
        run_git.assert_called_once_with("status", "--porcelain", "--untracked-files=normal")

    def test_reports_an_unverified_reset_as_a_rollback_failure(self):
        reset_result = subprocess.CompletedProcess(
            args=["git", "reset", "--hard", "previous-sha"],
            returncode=0,
            stdout="",
            stderr="",
        )
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "unknown",
                get_deploy_config_hash=lambda: _CONFIG_HASH,
                run_git=lambda *_args: reset_result,
            )
        )
        result = rollback_deploy_checkout("previous-sha")

        assert result.success is False
        assert "could not verify" in result.error

    def test_empty_previous_sha_is_rejected(self):
        result = rollback_deploy_checkout("")

        assert result == RollbackResult(
            success=False,
            error="no previous deploy SHA was recorded",
        )

    def test_reports_when_git_reset_cannot_run(self):
        run_git = MagicMock(
            side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                OSError("git unavailable"),
            ]
        )
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "unused",
                get_deploy_config_hash=lambda: _CONFIG_HASH,
                run_git=run_git,
            )
        )

        result = rollback_deploy_checkout("previous-sha")

        assert result.success is False
        assert result.error == "git reset could not run: git unavailable"

    def test_reports_a_failed_git_reset(self):
        run_git = MagicMock(
            side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess(
                    args=["git"], returncode=1, stdout="", stderr="permission denied"
                ),
            ]
        )
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "unused",
                get_deploy_config_hash=lambda: _CONFIG_HASH,
                run_git=run_git,
            )
        )

        result = rollback_deploy_checkout("previous-sha")

        assert result.success is False
        assert result.error == "permission denied"

    def test_captures_the_configured_deploy_revision(self):
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=lambda: "current-sha",
                get_deploy_config_hash=lambda: "current-config",
                run_git=MagicMock(),
            )
        )

        revision = current_deploy_revision()

        assert revision.commit_sha == "current-sha"
        assert revision.config_hash == "current-config"

    @pytest.mark.parametrize(
        ("change_kind", "reason"),
        [
            (DeployChangeKind.CODE, "code change"),
            (DeployChangeKind.CONFIG, "config change"),
            (DeployChangeKind.CODE_AND_CONFIG, "code and config changes"),
            (DeployChangeKind.RESTART, "restart request"),
        ],
    )
    async def test_broadcasts_notification_with_short_sha_and_reason(
        self,
        deploy_dir: Path,
        change_kind: DeployChangeKind,
        reason: str,
    ):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="commit-sha-001",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=change_kind,
                data_dir=deploy_dir,
            )

        broadcast.assert_called_once()
        jid, text = broadcast.call_args[0]
        assert jid == "group@g.us"
        assert text == f"Deploying commit-s ({reason})... restarting now."

    async def test_skips_broadcast_when_no_chat_jid(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="",
                commit_sha="abc123",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CONFIG,
                data_dir=deploy_dir,
            )

        broadcast.assert_not_called()

    async def test_sends_sigterm_immediately_by_default(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill") as mock_kill:
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CODE_AND_CONFIG,
                data_dir=deploy_dir,
            )

        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    async def test_delays_sigterm_when_delay_specified(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with (
            patch("pynchy.host.orchestrator.deploy.os.kill") as mock_kill,
            patch("pynchy.host.orchestrator.deploy.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop_instance = mock_loop.return_value
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CONFIG,
                data_dir=deploy_dir,
                sigterm_delay=2.0,
            )

        # Should use call_later instead of immediate kill
        mock_kill.assert_not_called()
        mock_loop_instance.call_later.assert_called_once()
        delay_arg = mock_loop_instance.call_later.call_args[0][0]
        assert delay_arg == 2

    async def test_creates_parent_directories(self, deploy_dir: Path):
        """Continuation file path's parent dir should be created if missing."""
        broadcast = AsyncMock()

        # Remove the deploy_dir to simulate fresh install
        shutil.rmtree(deploy_dir)

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CODE,
                data_dir=deploy_dir,
            )

        assert (deploy_dir / "deploy_continuation.json").exists()

    async def test_handles_unknown_commit_sha(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="",
                config_hash=_CONFIG_HASH,
                previous_sha="",
                change_kind=DeployChangeKind.RESTART,
                data_dir=deploy_dir,
            )

        broadcast.assert_called_once()
        _, text = broadcast.call_args[0]
        assert "unknown" in text

    async def test_default_resume_prompt(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CONFIG,
                data_dir=deploy_dir,
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert isinstance(continuation["resume_prompt"], str)
        assert continuation["resume_prompt"]

    async def test_in_flight_turn_snapshot_written_to_continuation(self, deploy_dir: Path):
        """The deploy file includes diagnostic metadata for actual running work."""
        broadcast = AsyncMock()
        turn = InFlightTurn(
            turn_id="turn-1",
            chat_jid="team@g.us",
            group_folder="team",
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[{"content": "keep going"}],
            input_start_cursor="old",
            input_end_cursor="new",
            started_at="2026-07-14T10:00:00+00:00",
        )

        with (
            patch("pynchy.host.orchestrator.deploy.os.kill"),
            patch(
                "pynchy.state.api.get_in_flight_turns",
                new_callable=AsyncMock,
                return_value=[turn],
            ),
        ):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="admin-1@g.us",
                commit_sha="abc123",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CODE,
                data_dir=deploy_dir,
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert continuation["interrupted_turns"] == [
            {
                "turn_id": "turn-1",
                "chat_jid": "team@g.us",
                "work_kind": "interactive",
            }
        ]

    async def test_idle_saved_sessions_are_not_snapshotted(self, deploy_dir: Path):
        """A resumable agent thread alone must not be mistaken for running work."""
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="admin-1@g.us",
                commit_sha="abc",
                config_hash=_CONFIG_HASH,
                previous_sha="000",
                change_kind=DeployChangeKind.CODE,
                data_dir=deploy_dir,
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert continuation["interrupted_turns"] == []
        assert "active_sessions" not in continuation


class TestBuildContainerImage:
    """Tests for the bounded container-image build invocation."""

    def test_uses_the_short_default_timeout(self, tmp_path: Path) -> None:
        build_script = tmp_path / "src" / "pynchy" / "agent" / "build.sh"
        build_script.parent.mkdir(parents=True)
        build_script.touch()
        completed = subprocess.CompletedProcess(
            args=[str(build_script)], returncode=0, stdout="", stderr=""
        )

        with patch("pynchy.host.orchestrator.deploy.subprocess.run", return_value=completed) as run:
            result = build_container_image(tmp_path)

        assert result.success is True
        assert run.call_args.kwargs["timeout"] == 240

    def test_skips_when_the_build_script_is_missing(self, tmp_path: Path) -> None:
        result = build_container_image(tmp_path)

        assert result == BuildResult(success=True, skipped=True)

    def test_returns_build_stderr_when_the_script_fails(self, tmp_path: Path) -> None:
        build_script = tmp_path / "src" / "pynchy" / "agent" / "build.sh"
        build_script.parent.mkdir(parents=True)
        build_script.touch()
        completed = subprocess.CompletedProcess(
            args=[str(build_script)], returncode=1, stdout="", stderr="image failed\n"
        )

        with patch("pynchy.host.orchestrator.deploy.subprocess.run", return_value=completed):
            result = build_container_image(tmp_path)

        assert result == BuildResult(success=False, stderr="image failed\n")
