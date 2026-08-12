"""Public origin-drift state transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.git_ops.api import sync_poll

if TYPE_CHECKING:
    from pathlib import Path


class _RecordingGitSyncDeps:
    def __init__(self) -> None:
        self.deploy_calls: list[tuple[str, bool]] = []

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))

    async def broadcast_host_message(self, _jid: str, _text: str) -> None: ...

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None: ...

    async def wake_worktree_conflict(self, _jid: str) -> None: ...

    def has_active_session(self, _group_folder: str) -> bool:
        return False

    def workspaces(self) -> dict:
        return {}


@pytest.mark.asyncio
async def test_missing_origin_revision_leaves_state_unchanged(tmp_path: Path):
    deps = _RecordingGitSyncDeps()
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin", deployed_sha="deployed", config_hash="cfg"
    )

    with patch("pynchy.host.git_ops.sync_poll.host_get_origin_main_sha", return_value=None):
        changed = await sync_poll.check_origin_drift(tmp_path, state, None, deps, auto_deploy=True)

    assert changed is False
    assert state.last_origin_sha == "old-origin"


@pytest.mark.asyncio
async def test_failed_origin_update_does_not_advance_baseline(tmp_path: Path):
    deps = _RecordingGitSyncDeps()
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin",
        deployed_sha="deployed",
        config_hash="cfg",
        local_head="local",
    )

    with (
        patch(
            "pynchy.host.git_ops.sync_poll.host_get_origin_main_sha",
            return_value="new-origin",
        ),
        patch("pynchy.host.git_ops.sync_poll.host_update_main", return_value=False),
    ):
        changed = await sync_poll.check_origin_drift(tmp_path, state, None, deps, auto_deploy=True)

    assert changed is False
    assert state.last_origin_sha == "old-origin"


@pytest.mark.asyncio
async def test_manual_update_offer_advances_origin_baseline(tmp_path: Path):
    deps = _RecordingGitSyncDeps()
    offer_update = AsyncMock(return_value=True)
    deps.offer_update = offer_update
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin", deployed_sha="deployed", config_hash="cfg"
    )

    with patch(
        "pynchy.host.git_ops.sync_poll.host_get_origin_main_sha",
        return_value="new-origin",
    ):
        changed = await sync_poll.check_origin_drift(tmp_path, state, None, deps, auto_deploy=False)

    assert changed is False
    assert state.last_origin_sha == "new-origin"
    offer_update.assert_awaited_once_with("new-origin")


@pytest.mark.asyncio
async def test_manual_update_without_offer_capability_retries_next_poll(tmp_path: Path):
    deps = _RecordingGitSyncDeps()
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin", deployed_sha="deployed", config_hash="cfg"
    )

    with patch(
        "pynchy.host.git_ops.sync_poll.host_get_origin_main_sha",
        return_value="new-origin",
    ):
        changed = await sync_poll.check_origin_drift(tmp_path, state, None, deps, auto_deploy=False)

    assert changed is False
    assert state.last_origin_sha == "old-origin"


@pytest.mark.asyncio
async def test_successful_pull_without_deploy_advances_deployed_baseline(tmp_path: Path):
    deps = _RecordingGitSyncDeps()
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin",
        deployed_sha="deployed-sha",
        config_hash="cfg",
        local_head="local-sha",
    )

    with (
        patch(
            "pynchy.host.git_ops.sync_poll.host_get_origin_main_sha",
            return_value="origin-new",
        ),
        patch("pynchy.host.git_ops.sync_poll.host_update_main", return_value=True),
        patch("pynchy.host.git_ops.sync_poll.get_local_head_sha", return_value="pulled-head"),
        patch("pynchy.host.git_ops.sync_poll.needs_deploy", return_value=False),
    ):
        changed = await sync_poll.check_origin_drift(tmp_path, state, None, deps, auto_deploy=True)

    assert changed is False
    assert state.last_origin_sha == "origin-new"
    assert state.deployed_sha == "pulled-head"
    assert deps.deploy_calls == []
