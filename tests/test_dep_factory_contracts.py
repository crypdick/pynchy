"""Behavioral contracts for the host dependency adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullChannel, make_host_action_catalog, make_settings

import pynchy.host.orchestrator.dep_factory as dep_factory
from pynchy.config.api import BuiltinTool, JobConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.ipc.protocol import CreatePeriodicAgentRequest
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.redaction import GatewayRedactionPosture
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
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


def test_http_adapter_dispatches_scheduled_workflow() -> None:
    app = PynchyApp()
    task = ScheduledTask(
        id="task-1234567890",
        group_folder="project",
        chat_jid="discord:project",
        prompt="run",
        schedule_type="cron",
        schedule_value="0 2 * * *",
        session_policy=SessionPolicy.CONTINUE,
    )
    names: list[str | None] = []

    def capture(coro, *, name):
        coro.close()
        names.append(name)

    with (
        patch.object(
            dep_factory,
            "start_scheduled_agent_task_workflow",
            new_callable=AsyncMock,
        ) as start_workflow,
        patch.object(dep_factory, "create_background_task", side_effect=capture),
    ):
        dep_factory.make_http_deps(app).dispatch_scheduled_task(task)

    start_workflow.assert_called_once_with(task)
    assert names == ["webhook-task-task-1234567890"]


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
    app.retire_workspace_runtime = AsyncMock()

    with patch.object(dep_factory, "get_settings", return_value=settings):
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
    app.retire_workspace_runtime.assert_awaited_once_with("project")


@pytest.mark.asyncio
async def test_app_runtime_retirement_clears_state_before_artifact_cleanup(tmp_path: Path) -> None:
    app = PynchyApp()
    app.sessions["project"] = "session-1"
    app.queue.clear_pending_tasks = MagicMock()
    app.queue.clear_pending_messages = MagicMock()
    app.queue.stop_active_process_for_control = AsyncMock()
    settings = _settings(tmp_path)

    with (
        patch("pynchy.host.orchestrator.app.get_settings", return_value=settings),
        patch("pynchy.host.orchestrator.app.destroy_session", new_callable=AsyncMock) as destroy,
        patch("pynchy.host.orchestrator.app.clear_session", new_callable=AsyncMock) as clear,
        patch("pynchy.host.orchestrator.app.cleanup_workspace_artifacts") as cleanup,
    ):
        await app.retire_workspace_runtime("project")

    destroy.assert_awaited_once_with("project")
    clear.assert_awaited_once_with("project")
    cleanup.assert_called_once_with(
        "project",
        data_dir=settings.data_dir,
        groups_dir=settings.groups_dir,
        worktrees_dir=settings.worktrees_dir,
        git=dep_factory.run_git,
    )
    assert "project" not in app.sessions
    assert "project" in app.session_cleared


@pytest.mark.asyncio
async def test_app_reclaims_unowned_workspace_artifacts_at_startup(tmp_path: Path) -> None:
    app = PynchyApp()
    settings = _settings(tmp_path)
    folder = "project__thread_discord-channel-orphan"
    artifact = settings.data_dir / "sessions" / folder
    artifact.mkdir(parents=True)

    with (
        patch("pynchy.host.orchestrator.app.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_sessions",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_in_flight_turns",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await app.reclaim_orphaned_workspace_artifacts([])

    assert not artifact.exists()


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
    app.channels = [NullChannel()]
    with (
        patch.object(dep_factory, "get_settings", return_value=settings),
        patch.object(dep_factory, "session_handler") as session_handler,
    ):
        session_handler.clear_durable_context = AsyncMock()
        deps = dep_factory.make_ipc_deps(app)
        assert deps.workspaces() is app.workspaces
        assert deps.channels() is app.channels
        assert deps.get_active_sessions() == {profile.jid: "session-1"}
        assert deps.has_active_session("project")
        assert not deps.has_active_session("missing")
        app.sessions["project"] = ""
        assert deps.has_active_session("project")
        assert deps.get_active_sessions() == {}
        app.sessions["project"] = "session-1"
        app.session_cleared.add("project")
        assert not deps.has_active_session("project")
        assert deps.get_active_sessions() == {}
        assert deps.connection_statuses() == {}
        await deps.clear_session("project")
        deps.write_groups_snapshot("project", [{"jid": "x"}], {profile.jid}, is_admin=False)
        with pytest.raises(RuntimeError, match="no longer exists"):
            await deps.clear_session("missing")

    session_handler.clear_durable_context.assert_awaited_once_with(app, profile)
    snapshot = settings.data_dir / "ipc" / "project" / "available_groups.json"
    assert snapshot.exists()


def test_ipc_adapter_enqueues_interactive_turn() -> None:
    app = PynchyApp()
    with patch.object(dep_factory, "_schedule_interactive_turn") as schedule:
        dep_factory.make_ipc_deps(app).enqueue_message_check("discord:project")

    schedule.assert_called_once_with(app, "discord:project")


def test_ipc_adapter_publishes_permanent_capability_before_return(tmp_path: Path) -> None:
    app = PynchyApp()

    def update_policy(group_folder, capability_id, *, publish) -> None:
        assert (group_folder, capability_id) == ("calendar", "calendar.event.list")
        assert publish(tmp_path) == "pushed"

    with (
        patch("pynchy.host.orchestrator.app.get_settings", return_value=_settings(tmp_path)),
        patch.object(
            dep_factory.workspace_config,
            "update_workspace_capability_policy",
            side_effect=update_policy,
        ),
        patch(
            "pynchy.host.git_ops.api.sync_personalization_repo",
            side_effect=["idle", "pushed"],
        ) as publish,
        patch("pynchy.host.orchestrator.app.reset_settings") as reset_settings,
    ):
        dep_factory.make_ipc_deps(app).persist_capability_approval(
            "calendar", "calendar.event.list"
        )

    assert [call.args[0] for call in publish.call_args_list] == [tmp_path, tmp_path]
    reset_settings.assert_called_once_with()


def test_ipc_adapter_rejects_permanent_capability_when_checkout_cannot_sync(
    tmp_path: Path,
) -> None:
    app = PynchyApp()

    with (
        patch("pynchy.host.orchestrator.app.get_settings", return_value=_settings(tmp_path)),
        patch.object(
            dep_factory.workspace_config,
            "update_workspace_capability_policy",
        ) as update_policy,
        patch(
            "pynchy.host.git_ops.api.sync_personalization_repo",
            return_value="failed",
        ),
        pytest.raises(ValueError, match="Could not prepare personalization repository"),
    ):
        dep_factory.make_ipc_deps(app).persist_capability_approval(
            "calendar", "calendar.event.list"
        )

    update_policy.assert_not_called()


def test_ipc_adapter_projects_skill_access_status(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    deps = dep_factory.make_ipc_deps(PynchyApp())
    resolved = MagicMock(denied_skills={"blocked"}, skills=["granted"])

    with patch.object(dep_factory, "get_settings", return_value=settings):
        with patch.object(dep_factory, "find_personalized_skill_dir", return_value=None):
            assert deps.skill_access_status("project", "missing") == "unknown"

        with (
            patch.object(dep_factory, "find_personalized_skill_dir", return_value=object()),
            patch.object(dep_factory.workspace_config, "load_resolved_config", return_value=None),
        ):
            assert deps.skill_access_status("project", "unavailable") == "unavailable"

        with (
            patch.object(dep_factory, "find_personalized_skill_dir", return_value=object()),
            patch.object(
                dep_factory.workspace_config, "load_resolved_config", return_value=resolved
            ),
        ):
            assert deps.skill_access_status("project", "blocked") == "denied"

        with (
            patch.object(dep_factory, "find_personalized_skill_dir", return_value=object()),
            patch.object(
                dep_factory.workspace_config, "load_resolved_config", return_value=resolved
            ),
            patch.object(dep_factory, "parse_skill_tier", return_value=("granted", "default")),
            patch.object(dep_factory, "is_skill_selected", return_value=True),
        ):
            assert deps.skill_access_status("project", "granted") == "granted"

        with (
            patch.object(dep_factory, "find_personalized_skill_dir", return_value=object()),
            patch.object(
                dep_factory.workspace_config, "load_resolved_config", return_value=resolved
            ),
            patch.object(dep_factory, "parse_skill_tier", return_value=("available", "default")),
            patch.object(dep_factory, "is_skill_selected", return_value=False),
        ):
            assert deps.skill_access_status("project", "available") == "available"


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
        patch.object(dep_factory, "start_deploy_workflow", new_callable=AsyncMock) as start_deploy,
        patch.object(dep_factory, "get_head_sha", return_value="head"),
        patch.object(dep_factory, "get_deploy_config_hash", return_value="config"),
        patch.object(app, "broadcast_host_message", new_callable=AsyncMock) as broadcast,
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
async def test_trigger_deploy_without_admin_workspace_skips_notification(
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    settings = _settings(tmp_path, admin_workspace=None)

    with (
        patch.object(dep_factory, "get_settings", return_value=settings),
        patch.object(dep_factory, "start_deploy_workflow", new_callable=AsyncMock) as start_deploy,
        patch.object(dep_factory, "get_head_sha", return_value="head"),
        patch.object(dep_factory, "get_deploy_config_hash", return_value="config"),
        patch.object(app, "broadcast_host_message", new_callable=AsyncMock) as broadcast,
    ):
        await dep_factory.make_ipc_deps(app).trigger_deploy("old")

    broadcast.assert_not_awaited()
    assert not start_deploy.await_args.args[0].chat_jid


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
async def test_ipc_automation_adapter_persists_and_projects_config_definitions(
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    app.sync_personalization = MagicMock(return_value="idle")
    settings = _settings(tmp_path)
    own = JobConfig(schedule="0 * * * *", workspace="project", prompt="run")
    foreign = JobConfig(schedule="0 * * * *", workspace="other", prompt="run")
    settings.jobs = {"own": own, "foreign": foreign}

    with (
        patch.object(dep_factory, "get_settings", return_value=settings),
        patch.object(dep_factory.workspace_config, "reset_settings") as reset,
    ):
        deps = dep_factory.make_ipc_deps(app)
        assert await deps.get_automation_status(source_group="project", is_admin=False) == [
            {"name": "own", **own.model_dump(exclude_none=True)}
        ]
        assert await deps.get_automation_definition(
            "own", source_group="project", is_admin=False
        ) == {"name": "own", **own.model_dump(exclude_none=True)}
        assert (
            await deps.get_automation_definition("foreign", source_group="project", is_admin=False)
            is None
        )
        assert (
            await deps.get_automation_definition("missing", source_group="project", is_admin=True)
            is None
        )

        settings.jobs = {}
        await deps.mutate_automation(
            "create_automation",
            "daily",
            {"schedule": "0 * * * *", "workspace": "workspace", "prompt": "run"},
        )
        await deps.mutate_automation("update_automation", "daily", {"prompt": "changed"})
        await deps.mutate_automation("pause_automation", "daily", {})
        await deps.mutate_automation("resume_automation", "daily", {})
        await deps.mutate_automation("delete_automation", "daily", {})

        settings.jobs = {"exists": own}
        with pytest.raises(ValueError, match="already exists"):
            await deps.mutate_automation(
                "create_automation",
                "exists",
                {"schedule": "0 * * * *", "workspace": "host", "prompt": "run"},
            )
        settings.jobs = {}
        with pytest.raises(ValueError, match="Unknown automation workspace"):
            await deps.mutate_automation(
                "create_automation",
                "unknown-workspace",
                {"schedule": "0 * * * *", "workspace": "missing", "prompt": "run"},
            )
        with pytest.raises(ValueError, match="Unknown automation operation"):
            await deps.mutate_automation("unknown", "daily", {})

        app.sync_personalization.return_value = "failed"
        with pytest.raises(RuntimeError, match="not published"):
            await deps.mutate_automation(
                "create_automation",
                "publication-failure",
                {"schedule": "0 * * * *", "workspace": "workspace", "prompt": "run"},
            )

    assert reset.call_count == 6


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
        assert deps.is_shutting_down() is False

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
                "managed": True,
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
