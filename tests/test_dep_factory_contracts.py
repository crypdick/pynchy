"""Behavioral contracts for the host dependency adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullChannel, make_host_action_catalog, make_settings

import pynchy.host.orchestrator.dep_factory as dep_factory
from pynchy.config.api import BuiltinTool, ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.ipc.protocol import CreatePeriodicAgentRequest
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.redaction import GatewayRedactionPosture
from pynchy.workspace.api import WorkspaceProfile, WorkspaceSecurity

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _Notifications:
    admin_workspace: str | None


@dataclass(frozen=True)
class _CommandCenter:
    connection: str | None


@dataclass(frozen=True)
class _PolicyDecision:
    allowed: bool
    reason: str
    needs_human: bool
    needs_cop: bool


@dataclass(frozen=True)
class _DockerResult:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class _BuiltinGateway:
    port: int
    key: str
    redaction_posture: GatewayRedactionPosture


def _workspace(folder: str = "admin", *, is_admin: bool = True) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=f"discord:channel:{folder}",
        name=folder.title(),
        folder=folder,
        trigger="@pynchy",
        is_admin=is_admin,
    )


def _settings(tmp_path: Path, *, admin_workspace: str | None = None):
    return make_settings(
        data_dir=tmp_path,
        project_root=tmp_path,
        groups_dir=tmp_path / "groups",
        notifications=_Notifications(admin_workspace),
        command_center=_CommandCenter(None),
        tools={"shell": BuiltinTool(type="builtin")},
        profiles={"default": ProfileConfig(tools=["shell"])},
        workspaces={"workspace": WorkspaceConfig(profiles=["default"])},
    )


@pytest.mark.asyncio
async def test_http_adapter_exposes_runtime_callbacks_and_plugin_guard(tmp_path: Path) -> None:
    app = PynchyApp()
    profile = _workspace("project", is_admin=False)
    app.workspaces[profile.jid] = profile
    settings = _settings(tmp_path)
    inbound = AsyncMock()
    app.on_inbound = inbound

    with patch.object(dep_factory, "get_settings", return_value=settings):
        deps = dep_factory.make_http_deps(app)
        assert deps.get_workspace("project") is profile
        assert deps.get_workspace("missing") is None
        assert deps.channels() is app.channels
        assert deps.workspaces() is app.workspaces
        assert not deps.admin_chat_jid()
        with pytest.raises(RuntimeError, match="Plugin manager"):
            deps.get_plugin_manager()

        app.plugin_manager = object()
        assert deps.get_plugin_manager() is app.plugin_manager
        await deps.ingest_runtime_harness_message(profile.jid, "hello")

    message = inbound.await_args.args[1]
    assert message.content == "hello"
    assert message.metadata == {"source": "runtime_harness"}


@pytest.mark.asyncio
async def test_http_adapter_delegates_workspace_and_session_operations(tmp_path: Path) -> None:
    app = PynchyApp()
    settings = _settings(tmp_path)
    profile = _workspace("project", is_admin=False)
    app.sessions["project"] = "session-1"
    app.workspaces[profile.jid] = profile
    app.register_workspace = AsyncMock()
    app.unregister_workspace = AsyncMock()
    app.rebind_workspace = AsyncMock()
    app.bind_routed_session = AsyncMock()
    app.queue.clear_pending_tasks = lambda _runtime: None
    app.queue.clear_pending_messages = lambda _runtime: None
    app.queue.stop_active_process_for_control = AsyncMock()

    with (
        patch.object(dep_factory, "get_settings", return_value=settings),
        patch.object(dep_factory, "destroy_session", new_callable=AsyncMock) as destroy,
        patch.object(dep_factory, "clear_session", new_callable=AsyncMock) as clear,
    ):
        deps = dep_factory.make_http_deps(app)
        await deps.register_workspace(profile)
        await deps.unregister_workspace(profile.jid)
        await deps.rebind_workspace(profile)
        await deps.bind_session("project", "session-2")
        await deps.retire_conversation_runtime("project")

    app.register_workspace.assert_awaited_once_with(profile)
    app.unregister_workspace.assert_awaited_once_with(profile.jid)
    app.rebind_workspace.assert_awaited_once_with(profile)
    app.bind_routed_session.assert_awaited_once_with("project", "session-2")
    destroy.assert_awaited_once_with("project")
    clear.assert_awaited_once_with("project")
    assert "project" in app.session_cleared


def test_http_capability_adapter_projects_config_and_policy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch.object(dep_factory, "get_settings", return_value=settings):
        operations = dep_factory.make_http_deps(PynchyApp()).capability_status_operations
        configuration = operations.workspace_configuration("workspace")
        assert configuration is not None
        assert configuration.enabled_tools == {"shell"}
        assert operations.workspace_configuration("missing") is None

        action = make_host_action_catalog("shell", handler=AsyncMock()).actions[0]
        expected = _PolicyDecision(
            allowed=False,
            reason="needs approval",
            needs_human=True,
            needs_cop=False,
        )
        with patch.object(
            dep_factory,
            "evaluate_host_action_policy",
            return_value=expected,
        ) as evaluate:
            decision = operations.evaluate_action_policy(action, WorkspaceSecurity())

    assert decision.allowed is False
    assert decision.reason == "needs approval"
    assert decision.approval_required is True
    assert decision.cop_review_required is False
    assert evaluate.call_args.args[0] is action


@pytest.mark.asyncio
async def test_ipc_adapter_projects_sessions_snapshot_and_context_reset(tmp_path: Path) -> None:
    app = PynchyApp()
    settings = _settings(tmp_path)
    profile = _workspace("project", is_admin=False)
    app.workspaces[profile.jid] = profile
    app.sessions["project"] = "session-1"
    app.channels = []
    with (
        patch.object(dep_factory, "get_settings", return_value=settings),
        patch.object(dep_factory, "session_handler") as session_handler,
    ):
        session_handler.clear_durable_context = AsyncMock()
        deps = dep_factory.make_ipc_deps(app)
        assert deps.workspaces() is app.workspaces
        assert deps.get_active_sessions() == {profile.jid: "session-1"}
        assert deps.connection_statuses() == {}
        await deps.clear_session("project")
        deps.write_groups_snapshot("project", [{"jid": "x"}], {profile.jid}, is_admin=False)
        with pytest.raises(RuntimeError, match="no longer exists"):
            await deps.clear_session("missing")

    session_handler.clear_durable_context.assert_awaited_once_with(app, profile)
    snapshot = settings.data_dir / "ipc" / "project" / "available_groups.json"
    assert snapshot.exists()


@pytest.mark.asyncio
async def test_ipc_adapter_deploy_paths_and_periodic_agent(tmp_path: Path) -> None:
    app = PynchyApp()
    profile = _workspace()
    app.workspaces[profile.jid] = profile
    settings = _settings(tmp_path, admin_workspace="admin")
    settings.command_center = _CommandCenter("discord")

    class CreateGroupChannel(NullChannel):
        name = "discord"

        def __init__(self) -> None:
            self.create_group = AsyncMock(return_value="discord:new")

    channel = CreateGroupChannel()
    app.channels = [channel]
    request = CreatePeriodicAgentRequest(
        name="nightly",
        profile="default",
        schedule="0 2 * * *",
        prompt="Check the project.",
        claude_md="# Nightly",
        chat=None,
        memory_enabled=True,
    )
    created_tasks: list[object] = []
    create_task = AsyncMock()

    with (
        patch.object(dep_factory, "get_settings", return_value=settings),
        patch.object(dep_factory.workspace_config, "add_workspace_to_toml") as add_workspace,
        patch.object(dep_factory.workspace_config, "add_job_to_toml") as add_job,
        patch.object(dep_factory, "create_task", create_task),
        patch.object(
            dep_factory,
            "create_background_task",
            lambda coro, name: (created_tasks.append(name), coro.close()),
        ),
        patch(
            "pynchy.host.orchestrator.adapters.create_background_task",
            lambda coro, name: (created_tasks.append(name), coro.close()),
        ),
        patch.object(dep_factory, "start_deploy_workflow", new_callable=AsyncMock) as start_deploy,
        patch.object(dep_factory, "get_head_sha", return_value="head"),
        patch.object(dep_factory, "get_deploy_config_hash", return_value="config"),
        patch.object(
            app.host_broadcaster, "broadcast_host_message", new_callable=AsyncMock
        ) as broadcast,
    ):
        deps = dep_factory.make_ipc_deps(app)
        await deps.create_periodic_agent(request)
        await deps.request_deploy(
            chat_jid="discord:explicit",
            commit_sha="abc",
            rebuild=False,
            resume_prompt="resume",
        )
        await deps.trigger_deploy("old", rebuild=False)

    add_workspace.assert_called_once()
    add_job.assert_called_once()
    channel.create_group.assert_awaited_once_with("nightly")
    create_task.assert_awaited_once()
    assert created_tasks == ["register-workspace-nightly"]
    assert start_deploy.await_count == 2
    assert start_deploy.await_args_list[0].args[0].chat_jid == "discord:explicit"
    assert start_deploy.await_args_list[1].args[0].chat_jid == profile.jid
    broadcast.assert_awaited_once_with(
        profile.jid, "Code/config changed — starting deploy workflow..."
    )


@pytest.mark.asyncio
async def test_ipc_adapter_handles_missing_command_center_and_target(tmp_path: Path) -> None:
    app = PynchyApp()
    settings = _settings(tmp_path)
    with patch.object(dep_factory, "get_settings", return_value=settings):
        deps = dep_factory.make_ipc_deps(app)
        request = CreatePeriodicAgentRequest(
            name="ignored",
            profile="default",
            schedule="0 2 * * *",
            prompt="prompt",
            claude_md="docs",
            chat=None,
            memory_enabled=False,
        )
        await deps.create_periodic_agent(request)
        await deps.request_deploy(
            chat_jid=None,
            commit_sha="abc",
            rebuild=True,
            resume_prompt="resume",
        )


@pytest.mark.asyncio
async def test_status_adapter_reports_queue_gateway_container_and_counts(tmp_path: Path) -> None:
    app = PynchyApp()
    settings = _settings(tmp_path)
    profile = _workspace("project", is_admin=False)
    app.workspaces[profile.jid] = profile
    app.sessions["project"] = "session-1"

    class Channel(NullChannel):
        name = "discord"

        def is_connected(self) -> bool:
            return True

    app.channels = [Channel()]
    app.queue.snapshot = lambda: {
        "project": {"status": "running"},
        "_meta": {"active_count": 1, "waiting_count": 2},
    }
    app.connection_runtime_owner.status = lambda: {"discord": True}
    settings.container.max_concurrent = 3

    with patch.object(dep_factory, "get_settings", return_value=settings):
        deps = dep_factory.make_status_deps(app)
        assert deps.get_channel_status() == {"discord": True}
        assert deps.get_connection_status() == {"discord": True}
        assert deps.get_queue_snapshot() == {
            "active_containers": 1,
            "max_concurrent": 3,
            "groups_waiting": 2,
            "per_group": {"project": {"status": "running"}},
        }
        assert deps.get_active_sessions_count() == 1
        assert deps.get_workspace_count() == 1
        assert deps.get_speech_synthesizer() is None
        assert deps.get_gateway_info() == {"mode": "none"}

        builtin = _BuiltinGateway(
            port=4010,
            key="builtin-key",
            redaction_posture=GatewayRedactionPosture.ENFORCED,
        )
        with patch.object(dep_factory.gateway_manager, "get_gateway", return_value=builtin):
            assert deps.get_gateway_info() == {
                "mode": "builtin",
                "port": 4010,
                "key": "builtin-key",
                "redaction": "enforced",
            }

        result = _DockerResult(returncode=0, stdout="running\n")
        with patch.object(dep_factory, "run_docker", new_callable=AsyncMock, return_value=result):
            assert await deps.get_container_state("agent") == "running"
        with patch.object(dep_factory, "run_docker", side_effect=FileNotFoundError):
            assert await deps.get_container_state("agent") == "not_found"
        with patch.object(
            dep_factory,
            "run_docker",
            return_value=_DockerResult(returncode=1, stdout=""),
        ):
            assert await deps.get_container_state("agent") == "not_found"


def test_git_sync_adapter_delegates_live_app_state() -> None:
    app = PynchyApp()
    profile = _workspace("project", is_admin=False)
    app.workspaces[profile.jid] = profile
    app.sessions["project"] = "session-1"
    deps = dep_factory.make_git_sync_deps(app)

    assert deps.workspaces() is app.workspaces
    assert deps.has_active_session("project") is True
    assert deps.has_active_session("missing") is False
