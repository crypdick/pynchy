"""Tests for deploy logic.

Tests finalize_deploy() which coordinates continuation file writing,
user notification, and process restart via SIGTERM. Errors here could
leave the service in a broken state or lose deploy context.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.deploy import finalize_deploy


@contextlib.contextmanager
def _patch_settings(*, data_dir: Path):
    s = Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )
    s.__dict__["data_dir"] = data_dir
    with patch("pynchy.deploy.get_settings", return_value=s):
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

        with patch("pynchy.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="commit-sha-001",
                previous_sha="previous-sha-001",
                session_id="session-42",
                resume_prompt="Deploy complete.",
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert continuation["chat_jid"] == "group@g.us"
        assert continuation["commit_sha"] == "commit-sha-001"
        assert continuation["previous_commit_sha"] == "previous-sha-001"
        assert continuation["session_id"] == "session-42"
        assert continuation["resume_prompt"] == "Deploy complete."

    async def test_broadcasts_notification_with_short_sha(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.deploy.os.kill"):
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

        with patch("pynchy.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="",
                commit_sha="abc123",
                previous_sha="000",
            )

        broadcast.assert_not_called()

    async def test_sends_sigterm_immediately_by_default(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.deploy.os.kill") as mock_kill:
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
            patch("pynchy.deploy.os.kill") as mock_kill,
            patch("pynchy.deploy.asyncio.get_running_loop") as mock_loop,
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
        assert delay_arg == 2.0

    async def test_creates_parent_directories(self, deploy_dir: Path):
        """Continuation file path's parent dir should be created if missing."""
        broadcast = AsyncMock()

        # Remove the deploy_dir to simulate fresh install
        import shutil

        shutil.rmtree(deploy_dir)

        with patch("pynchy.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                previous_sha="000",
            )

        assert (deploy_dir / "deploy_continuation.json").exists()

    async def test_handles_unknown_commit_sha(self, deploy_dir: Path):
        broadcast = AsyncMock()

        with patch("pynchy.deploy.os.kill"):
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

        with patch("pynchy.deploy.os.kill"):
            await finalize_deploy(
                broadcast_host_message=broadcast,
                chat_jid="group@g.us",
                commit_sha="abc",
                previous_sha="000",
            )

        continuation = json.loads((deploy_dir / "deploy_continuation.json").read_text())
        assert "Deploy complete" in continuation["resume_prompt"]
