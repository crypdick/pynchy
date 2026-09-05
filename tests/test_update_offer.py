"""Tests for admin-approved repository updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from beartype import beartype

from pynchy.config.api import SchedulerConfig
from pynchy.deployments import (
    DeployClaim,
    DeployClaimStatus,
    DeployRevision,
)
from pynchy.host.orchestrator import update_offer
from pynchy.state import init_test_database, initialize_deployment_state
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pynchy.agent_protocol.api import AgentExecutionRuntime

_OLD_SHA = "a" * 40
_NEW_SHA = "b" * 40


def test_update_offer_answer_is_runtime_decoratable() -> None:
    """The public update-answer boundary remains instrumented by Beartype."""
    assert callable(beartype(update_offer.handle_update_offer_answer))


@dataclass
class _UpdateDeps:
    workspaces: dict[str, WorkspaceProfile]
    broadcast_host_message: AsyncMock
    admin_workspace: str | None
    agent_execution_runtime: AgentExecutionRuntime
    get_local_head_sha: Callable[[Path], str] = lambda _root: _OLD_SHA
    get_deploy_config_hash: Callable[[], str] = lambda: "old-config"
    host_update_main: Callable[[Path], bool] = lambda _root: True
    needs_deploy: Callable[[str, str], bool] = lambda _old, _new: True
    needs_container_rebuild: Callable[[str, str], bool] = lambda _old, _new: True


@dataclass
class _Runtime:
    project_root: Path


def _admin_workspace() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:42",
        name="admin",
        folder="admin",
        trigger="always",
        is_admin=True,
    )


def test_auto_deploy_requires_an_explicit_opt_in() -> None:
    """Fresh deployments do not mutate the checkout for a new remote revision."""
    assert not SchedulerConfig().auto_deploy


@pytest.mark.parametrize(
    "request_id",
    [
        "not-an-update",
        "host-update:short",
        "host-update:" + "a" * 65,
        "host-update:" + "g" * 40,
    ],
)
async def test_update_offer_ignores_invalid_callback_ids(request_id: str, tmp_path: Path) -> None:
    deps = _UpdateDeps(
        workspaces={},
        broadcast_host_message=AsyncMock(),
        admin_workspace=None,
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
    )

    assert not await update_offer.handle_update_offer_answer(request_id, {}, deps)


async def test_update_offer_ignores_unapproved_answer(tmp_path: Path) -> None:
    workspace = _admin_workspace()
    deps = _UpdateDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
        admin_workspace="admin",
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
    )

    assert await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": "No", "channel_id": "42"},
        deps,
    )
    deps.broadcast_host_message.assert_not_awaited()


async def test_update_offer_without_admin_workspace_is_handled_without_deploy(
    tmp_path: Path,
) -> None:
    deps = _UpdateDeps(
        workspaces={},
        broadcast_host_message=AsyncMock(),
        admin_workspace=None,
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
    )
    deps.host_update_main = AsyncMock()

    assert await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": ["Fetch and upgrade"]},
        deps,
    )
    deps.host_update_main.assert_not_awaited()


async def test_update_offer_reports_fetch_failure(tmp_path: Path) -> None:
    await init_test_database()
    workspace = _admin_workspace()
    deps = _UpdateDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
        admin_workspace="admin",
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
        host_update_main=lambda _root: False,
    )

    assert await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": ["Fetch and upgrade"], "channel_id": "42"},
        deps,
    )
    deps.broadcast_host_message.assert_awaited_once_with(
        workspace.jid,
        f"Could not fetch update {_NEW_SHA[:8]}; the running deployment was unchanged.",
    )


async def test_update_offer_updates_baseline_without_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await init_test_database()
    await initialize_deployment_state(DeployRevision(_OLD_SHA, "old-config"))
    workspace = _admin_workspace()
    deps = _UpdateDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
        admin_workspace="admin",
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
        get_local_head_sha=lambda _root: _NEW_SHA,
        needs_deploy=lambda _old, _new: False,
    )
    start_deploy = AsyncMock()
    monkeypatch.setattr(update_offer, "start_deploy_workflow", start_deploy)

    assert await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": ["Fetch and upgrade"], "channel_id": "42"},
        deps,
    )
    start_deploy.assert_not_awaited()
    deps.broadcast_host_message.assert_awaited_once_with(
        workspace.jid,
        f"Fetched update {_NEW_SHA[:8]}. No service restart was needed.",
    )


async def test_update_offer_reports_deploy_claim_not_started(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await init_test_database()
    await initialize_deployment_state(DeployRevision(_OLD_SHA, "old-config"))
    workspace = _admin_workspace()
    deps = _UpdateDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
        admin_workspace="admin",
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
        get_local_head_sha=lambda _root: _NEW_SHA,
        get_deploy_config_hash=lambda: "new-config",
    )
    monkeypatch.setattr(
        update_offer,
        "start_deploy_workflow",
        AsyncMock(return_value=DeployClaim(DeployClaimStatus.BUSY)),
    )

    assert await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": "Fetch and upgrade", "channel_id": "42"},
        deps,
    )
    deps.broadcast_host_message.assert_awaited_once_with(
        workspace.jid,
        f"Update {_NEW_SHA[:8]} was not started: busy.",
    )


async def test_accepted_offer_fetches_then_starts_deploy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Approval fetches the checkout before handing the new revision to Temporal."""
    await init_test_database()
    await initialize_deployment_state(DeployRevision(_OLD_SHA, "old-config"))
    workspace = _admin_workspace()
    deps = _UpdateDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
        admin_workspace="admin",
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
    )
    start_deploy = AsyncMock(return_value=DeployClaim(DeployClaimStatus.CLAIMED))
    deps.host_update_main = lambda _root: True
    deps.get_local_head_sha = lambda _root: _NEW_SHA
    deps.get_deploy_config_hash = lambda: "new-config"
    deps.needs_deploy = lambda _old, _new: True
    deps.needs_container_rebuild = lambda _old, _new: True
    monkeypatch.setattr(update_offer, "start_deploy_workflow", start_deploy)

    handled = await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": "Fetch and upgrade", "channel_id": "42"},
        deps,
    )

    assert handled
    request = start_deploy.await_args.args[0]
    assert request.commit_sha == _NEW_SHA
    assert request.previous_sha == _OLD_SHA
    assert request.rebuild
    assert request.reason == "approved_update"
    deps.broadcast_host_message.assert_awaited_once_with(
        workspace.jid,
        f"Updating Pynchy to {_NEW_SHA[:8]}...",
    )


async def test_offer_requires_approval_from_the_configured_admin_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A button callback from a different channel cannot deploy Pynchy."""
    workspace = _admin_workspace()
    deps = _UpdateDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
        admin_workspace="admin",
        agent_execution_runtime=cast("AgentExecutionRuntime", _Runtime(tmp_path)),
    )
    update_main = AsyncMock()
    deps.host_update_main = update_main

    handled = await update_offer.handle_update_offer_answer(
        f"host-update:{_NEW_SHA}",
        {"answer": "Fetch and upgrade", "channel_id": "not-admin"},
        deps,
    )

    assert handled
    update_main.assert_not_awaited()
