"""Tests for deploy logic.

Tests finalize_deploy() which coordinates continuation file writing,
user notification, and process restart via SIGTERM. Errors here could
leave the service in a broken state or lose deploy context.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.host.orchestrator.deploy import finalize_deploy
from pynchy.types import InFlightTurn, InFlightWorkKind

if TYPE_CHECKING:
    from pathlib import Path


@contextlib.contextmanager
def _patch_settings(*, data_dir: Path):
    s = make_settings(data_dir=data_dir)
    with (
        patch("pynchy.host.orchestrator.deploy.get_settings", return_value=s),
        # finalize_deploy() persists deploy metadata via set_router_state(),
        # which requires an initialized DB.  Mock it out for unit tests.
        # Patch on pynchy.state (the re-export) so the local import inside
        # finalize_deploy picks up the mock.
        patch("pynchy.state.set_router_state", new_callable=AsyncMock),
        patch(
            "pynchy.state.get_in_flight_turns",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        yield


@pytest.fixture
def deploy_dir(tmp_path: Path):
    """Patch settings data_dir for isolated deploy tests."""
    with _patch_settings(data_dir=tmp_path):
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
                previous_sha="previous-sha-001",
                resume_prompt="Deploy complete.",
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert continuation["chat_jid"] == "group@g.us"
        assert continuation["commit_sha"] == "commit-sha-001"
        assert continuation["previous_commit_sha"] == "previous-sha-001"
        assert continuation["resume_prompt"] == "Deploy complete."
        assert continuation["interrupted_turns"] == []
        assert "active_sessions" not in continuation

    async def test_broadcasts_notification_with_short_sha(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="commit-sha-001",
                previous_sha="000",
            )

        broadcast.assert_called_once()
        jid, text = broadcast.call_args[0]
        assert jid == "group@g.us"
        assert "commit-s" in text  # First 8 chars of SHA

    async def test_skips_broadcast_when_no_chat_jid(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="",
                commit_sha="abc123",
                previous_sha="000",
            )

        broadcast.assert_not_called()

    async def test_sends_sigterm_immediately_by_default(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill") as mock_kill:
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                previous_sha="000",
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
                previous_sha="000",
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
                previous_sha="000",
            )

        assert (deploy_dir / "deploy_continuation.json").exists()

    async def test_handles_unknown_commit_sha(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.host.orchestrator.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="",
                previous_sha="",
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
                previous_sha="000",
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
                "pynchy.state.get_in_flight_turns",
                new_callable=AsyncMock,
                return_value=[turn],
            ),
        ):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="admin-1@g.us",
                commit_sha="abc123",
                previous_sha="000",
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
                previous_sha="000",
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert continuation["interrupted_turns"] == []
        assert "active_sessions" not in continuation
