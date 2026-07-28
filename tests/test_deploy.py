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
import subprocess  # noqa: S404, RUF100 - tests construct completed process results without executing commands.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.orchestrator.deploy import (
    DeployGitRuntime,
    configure_deploy_git_runtime,
    finalize_deploy,
    rollback_deploy_checkout,
)
from pynchy.types import DeployChangeKind, InFlightTurn, InFlightWorkKind

_CONFIG_HASH = "config-hash-001"

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

        run_git.assert_called_once_with("reset", "--hard", "previous-sha")
        assert result.success is True
        assert result.actual_sha == "previous-sha-full"

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
        assert "Deploy complete" in continuation["resume_prompt"]

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
