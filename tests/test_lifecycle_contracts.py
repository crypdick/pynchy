"""Behavioral tests for lifecycle composition and rollback contracts."""

from __future__ import annotations

# allow: file-length -- lifecycle composition contracts share one fixture boundary.
import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pluggy
import pytest
from aiohttp import web
from conftest import make_settings

from pynchy.config.api import (
    DiscordConnectionConfig,
    PluginConfig,
    SlackConnectionConfig,
    WhatsAppConnectionConfig,
)
from pynchy.conversation.api import (
    Conversation,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator import lifecycle, linear_issue_controls
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.http_control import ControlPlaneRuntime, RequestRateLimiter
from pynchy.host.orchestrator.http_readiness import ControlPlaneReadiness
from pynchy.plugins.api import NewMessage
from pynchy.plugins.integrations.api import LinearIssueControl
from pynchy.workspace.api import WorkspaceProfile

SLACK_BOT_ENV = "SLACK_BOT_TOKEN"
SLACK_APP_ENV = "SLACK_APP_TOKEN"
DISCORD_BOT_ENV = "DISCORD_BOT_TOKEN"


class _ConnectionRuntime:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ready = False

    async def start(self, _context: object) -> None:
        self.ready = True

    async def close(self) -> None:
        self.ready = False

    def is_ready(self) -> bool:
        return self.ready


class _RecordingQueue:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True


def _completed_awaitable(value: Any = None) -> Any:
    async def _completed() -> Any:
        await asyncio.sleep(0)
        return value

    return _completed()


def _recovery() -> lifecycle.startup_handler.InterruptedTurnRecovery:
    return lifecycle.startup_handler.InterruptedTurnRecovery(
        turns=(),
        commit_sha="unknown",
        resume_prompt="Continuing after host restart.",
        had_deploy_continuation=False,
        deploy_revision=None,
        rolled_back=False,
        continuation_path=None,
    )


def _prepared_http_server() -> lifecycle.http_server.PreparedHttpServer:
    web_app = web.Application()
    return lifecycle.http_server.PreparedHttpServer(
        runner=web.AppRunner(web_app),
        runtime=ControlPlaneRuntime(
            bind_host="127.0.0.1",
            port=8484,
            unix_socket=None,
            public_bind=False,
            remote_auth_required=False,
            allow_remote_deploy=False,
            auth_token=None,
            rate_limiter=RequestRateLimiter(request_limit=10, window_seconds=60),
            audit_security_event=lambda *_args, **_kwargs: _completed_awaitable(),
        ),
        app=web_app,
        readiness=ControlPlaneReadiness(),
    )


def _workspace() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="always",
    )


def _conversation() -> Conversation:
    return Conversation(
        id=ConversationId("conversation-1"),
        workspace="health",
        subject=ConversationSubject(
            ConversationSubjectNamespace("linear:issue"),
            ConversationSubjectKey("issue-1"),
        ),
        session_id=None,
        created_at="2026-07-31T08:00:00Z",
        updated_at="2026-07-31T08:00:00Z",
    )


@pytest.mark.asyncio
async def test_linear_forum_guidelines_link_only_configured_workspace(monkeypatch) -> None:
    app = PynchyApp()
    root = WorkspaceProfile(
        jid="discord:channel:health-forum",
        name="Health",
        folder="health",
        trigger="@Pynchy",
    )
    app.workspaces[root.jid] = root
    ensure_link = AsyncMock()
    monkeypatch.setattr(linear_issue_controls, "ensure_forum_guidelines_linked", ensure_link)

    await linear_issue_controls.ensure_forum_guidelines(
        app,
        {
            "health": MagicMock(project={"url": "https://linear.app/acme/project/health"}),
            "missing": MagicMock(project={"url": "https://linear.app/acme/project/missing"}),
            "invalid": MagicMock(project={"url": None}),
        },
    )

    ensure_link.assert_awaited_once_with(
        app.channels, root.jid, "https://linear.app/acme/project/health"
    )


def _patch_run_app_tail(monkeypatch, app: PynchyApp, settings: object) -> AsyncMock:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        AsyncMock(return_value=_recovery()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", AsyncMock())
    monkeypatch.setattr(app, "catch_up_channels", AsyncMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", AsyncMock())
    message_loop = AsyncMock()
    monkeypatch.setattr(lifecycle, "start_message_loop", message_loop)
    return message_loop


@pytest.mark.asyncio
async def test_run_app_composes_connection_context_and_dispatches_callbacks(
    monkeypatch, tmp_path
) -> None:
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        connections={
            "slack": SlackConnectionConfig(
                bot_token_env=SLACK_BOT_ENV,
                app_token_env=SLACK_APP_ENV,
                chat={"general": {}},
            ),
            "discord": DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
            "phone": WhatsAppConnectionConfig(auth_db_path="auth/phone.db"),
        },
    )
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy")
    app.workspaces = {"slack:C123": _workspace()}
    app.on_inbound = AsyncMock()
    app.on_reaction = AsyncMock()
    app.on_ask_user_answer = AsyncMock()
    app.broadcast_to_channels = AsyncMock()
    channel = MagicMock()
    channel.connect = AsyncMock()
    captured: dict[str, Any] = {}
    scheduled: list[str | None] = []

    def capture_channels(_plugin_manager: object, context: object) -> list[object]:
        captured["context"] = context
        return [channel]

    def discard_background_task(coro, *, name: str | None = None) -> object:
        scheduled.append(name)
        coro.close()
        return MagicMock()

    monkeypatch.setattr(lifecycle, "load_channels", capture_channels)
    monkeypatch.setattr(lifecycle, "create_background_task", discard_background_task)
    monkeypatch.setattr(lifecycle.startup_handler, "validate_plugin_credentials", lambda _: [])
    monkeypatch.setattr(lifecycle.output_handler, "init_trace_batcher", MagicMock())
    monkeypatch.setattr(lifecycle.approval_handler, "handle_approval_command", AsyncMock())
    monkeypatch.setattr(lifecycle, "_reconcile_state", AsyncMock(return_value={}))
    monkeypatch.setattr(lifecycle, "_start_runtime_owners", AsyncMock())
    _patch_run_app_tail(monkeypatch, app, settings)

    await lifecycle.run_app(app)

    context = captured["context"]
    assert context.slack_connections["slack"].chat_names == ("general",)
    assert context.discord_connections["discord"].bot_token_env == DISCORD_BOT_ENV
    assert (
        context.whatsapp_connections["phone"].auth_db_path == (tmp_path / "auth/phone.db").resolve()
    )
    channel.connect.assert_awaited_once_with()

    context.on_message_callback("chat", NewMessage("id", "chat", "sender", "Name", "hello", "ts"))
    context.on_chat_metadata_callback("chat", "ts", "Chat")
    context.on_reaction_callback("chat", "ts", "sender", "👍")
    context.on_ask_user_answer_callback("request", {"answer": "yes"})
    context.on_approval_decision_callback("chat", "approve", "short", "sender")
    await context.send_message("chat", "reply")

    assert scheduled == [
        "on-inbound",
        "store-metadata",
        "on-reaction",
        "on-ask-user-answer",
        "on-approval-decision",
    ]
    app.broadcast_to_channels.assert_awaited_once()
    assert app.broadcast_to_channels.await_args.args[0] == "chat"
    assert app.broadcast_to_channels.await_args.args[1].content == "reply"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_count", [0, 1])
async def test_run_app_reconciles_workspaces_boards_and_connection_runtimes(
    monkeypatch, binding_count: int
) -> None:
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy")
    root = WorkspaceProfile(
        jid="discord:channel:project",
        name="Project",
        folder="group",
        trigger="always",
    )
    app.workspaces = {root.jid: root}
    settings = make_settings()
    runtimes = [_ConnectionRuntime("runtime")]
    boards = {
        "group": lifecycle.LinearWorkspaceBoard(
            team={},
            project={"url": "https://linear.app/acme/project/pynchy"},
            states={},
        )
    }
    reconcile_worktrees = MagicMock()
    reconcile_workspaces = AsyncMock()
    _patch_run_app_tail(monkeypatch, app, settings)
    monkeypatch.setattr(lifecycle, "_setup_channels", AsyncMock())
    monkeypatch.setattr(lifecycle.workspace_config, "get_repo_access_groups", lambda _: {"group"})
    monkeypatch.setattr(lifecycle, "reconcile_worktrees_at_startup", reconcile_worktrees)
    monkeypatch.setattr(lifecycle.workspace_config, "reconcile_workspaces", reconcile_workspaces)
    cleanup_artifacts = AsyncMock()
    monkeypatch.setattr(app, "reclaim_orphaned_workspace_artifacts", cleanup_artifacts)
    reconcile_bindings = AsyncMock(return_value=binding_count)
    monkeypatch.setattr(lifecycle, "get_all_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        lifecycle,
        "reconcile_scheduled_task_bindings",
        reconcile_bindings,
    )
    monkeypatch.setattr(
        lifecycle,
        "reconcile_linear_boards",
        AsyncMock(return_value=boards),
    )
    ensure_guidelines = AsyncMock()
    monkeypatch.setattr(linear_issue_controls, "ensure_forum_guidelines", ensure_guidelines)
    monkeypatch.setattr(lifecycle, "load_connection_runtimes", MagicMock(return_value=runtimes))
    monkeypatch.setattr(lifecycle, "_start_runtime_owners", AsyncMock())

    await lifecycle.run_app(app)

    reconcile_worktrees.assert_called_once_with(repo_groups={"group"})
    reconcile_workspaces.assert_awaited_once_with(
        workspaces=app.workspaces,
        channels=app.channels,
        register_fn=app.register_workspace,
        unregister_fn=app.unregister_workspace,
        rebind_fn=app.rebind_workspace,
        retire_fn=app.retire_workspace_runtime,
    )
    reconcile_bindings.assert_awaited_once_with([], app)
    cleanup_artifacts.assert_awaited_once_with([])
    ensure_guidelines.assert_awaited_once_with(app, boards)
    assert app.connection_runtime_owner.runtimes() == tuple(runtimes)


@pytest.mark.asyncio
async def test_linear_issue_control_targets_managed_forum_root(monkeypatch) -> None:
    app = PynchyApp()
    conversation = _conversation()
    ensured = MagicMock()
    ensured.profile.folder = "health__thread_conversation-1"
    ensured.control.binding.thread_jid = "discord:thread:issue-1"
    apply_state = AsyncMock(return_value=True)
    monkeypatch.setattr(
        linear_issue_controls,
        "apply_conversation_control_state",
        apply_state,
    )
    monkeypatch.setattr(
        linear_issue_controls,
        "get_conversation_control_binding",
        AsyncMock(return_value=None),
    )
    ensure_workspace = AsyncMock(return_value=ensured)
    monkeypatch.setattr(
        linear_issue_controls,
        "ensure_conversation_workspace",
        ensure_workspace,
    )
    ensure_policy = MagicMock()
    ensure_link = AsyncMock()
    monkeypatch.setattr(
        linear_issue_controls,
        "ensure_runtime_workspace_policy_owner",
        ensure_policy,
    )
    monkeypatch.setattr(linear_issue_controls, "ensure_thread_link_pinned", ensure_link)
    control = LinearIssueControl(
        issue_id="issue-1",
        workspace="health",
        parent_jid="discord:channel:health-forum",
        account_name="linear",
        title="[SYN-1] Restore sleep access",
        url="https://linear.app/acme/issue/SYN-1",
        updated_at="2026-07-31T09:00:00Z",
    )

    await linear_issue_controls.ensure_issue_control(app, control, conversation)

    apply_state.assert_awaited_once_with(
        conversation.id,
        closed=False,
        control_state_revision=control.updated_at,
    )
    request = ensure_workspace.await_args.args[1]
    assert request.parent_workspace == "health"
    assert request.parent_jid == "discord:channel:health-forum"
    assert request.title == control.title
    assert request.kind == "issue"
    ensure_policy.assert_called_once_with(ensured.profile.folder, "health")
    ensure_link.assert_awaited_once_with(
        app.channels, ensured.control.binding.thread_jid, control.url
    )


@pytest.mark.asyncio
async def test_linear_issue_control_reuses_matching_open_binding(monkeypatch) -> None:
    app = PynchyApp()
    conversation = _conversation()
    control = LinearIssueControl(
        issue_id="issue-1",
        workspace="health",
        parent_jid="discord:channel:health-forum",
        account_name="linear",
        title="[SYN-1] Restore sleep access",
        url="https://linear.app/acme/issue/SYN-1",
        updated_at="2026-07-31T09:00:00Z",
    )
    profile = WorkspaceProfile(
        jid="discord:thread:issue-1",
        name="Issue",
        folder="health__thread_issue-1",
        trigger="@Pynchy",
        added_at="2026-07-31T08:00:00Z",
    )
    app.workspaces[profile.jid] = profile
    binding = MagicMock(parent_jid=control.parent_jid, thread_jid=profile.jid, closed=False)
    apply_state = AsyncMock(return_value=True)
    ensure_workspace = AsyncMock()
    ensure_policy = MagicMock()
    ensure_link = AsyncMock()
    monkeypatch.setattr(linear_issue_controls, "apply_conversation_control_state", apply_state)
    monkeypatch.setattr(
        linear_issue_controls,
        "get_conversation_control_binding",
        AsyncMock(return_value=binding),
    )
    monkeypatch.setattr(linear_issue_controls, "ensure_conversation_workspace", ensure_workspace)
    monkeypatch.setattr(
        linear_issue_controls,
        "ensure_runtime_workspace_policy_owner",
        ensure_policy,
    )
    monkeypatch.setattr(linear_issue_controls, "ensure_thread_link_pinned", ensure_link)

    await linear_issue_controls.ensure_issue_control(app, control, conversation)

    apply_state.assert_awaited_once_with(
        conversation.id,
        closed=False,
        control_state_revision=control.updated_at,
    )
    ensure_workspace.assert_not_awaited()
    ensure_policy.assert_called_once_with(profile.folder, conversation.workspace)
    ensure_link.assert_awaited_once_with(app.channels, profile.jid, control.url)


@pytest.mark.asyncio
async def test_linear_issue_control_rebuilds_when_binding_workspace_is_missing(
    monkeypatch,
) -> None:
    app = PynchyApp()
    conversation = _conversation()
    control = LinearIssueControl(
        issue_id="issue-1",
        workspace="health",
        parent_jid="discord:channel:health-forum",
        account_name="linear",
        title="[SYN-1] Restore sleep access",
        url="https://linear.app/acme/issue/SYN-1",
        updated_at="2026-07-31T09:00:00Z",
    )
    ensured = MagicMock(
        profile=WorkspaceProfile(
            jid="discord:thread:replacement",
            name="Replacement",
            folder="health__thread_replacement",
            trigger="@Pynchy",
        )
    )
    ensured.control.binding.thread_jid = "discord:thread:replacement"
    ensure_workspace = AsyncMock(return_value=ensured)
    ensure_policy = MagicMock()
    ensure_link = AsyncMock()
    monkeypatch.setattr(linear_issue_controls, "apply_conversation_control_state", AsyncMock())
    monkeypatch.setattr(
        linear_issue_controls,
        "get_conversation_control_binding",
        AsyncMock(
            return_value=MagicMock(
                parent_jid=control.parent_jid,
                thread_jid="discord:thread:missing",
                closed=False,
            )
        ),
    )
    monkeypatch.setattr(linear_issue_controls, "ensure_conversation_workspace", ensure_workspace)
    monkeypatch.setattr(
        linear_issue_controls,
        "ensure_runtime_workspace_policy_owner",
        ensure_policy,
    )
    monkeypatch.setattr(linear_issue_controls, "ensure_thread_link_pinned", ensure_link)

    await linear_issue_controls.ensure_issue_control(app, control, conversation)

    ensure_workspace.assert_awaited_once()
    ensure_policy.assert_called_once_with(ensured.profile.folder, conversation.workspace)
    ensure_link.assert_awaited_once_with(
        app.channels, ensured.control.binding.thread_jid, control.url
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "github_plugin",
    [
        None,
        PluginConfig(
            enabled=True,
            options={
                "webhook_routes": [
                    {
                        "name": "repo",
                        "workspace": "admin",
                        "repository": "owner/repo",
                    }
                ]
            },
        ),
    ],
)
async def test_run_app_proves_control_plane_before_activation(
    monkeypatch, github_plugin, tmp_path
) -> None:
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy")
    app.workspaces = {"slack:C123": _workspace()}
    settings = make_settings(
        data_dir=tmp_path / "data",
        plugins={} if github_plugin is None else {"github": github_plugin},
    )
    prepared = _prepared_http_server()
    runtime = MagicMock()
    status_deps = MagicMock()
    http_deps = MagicMock()
    _patch_run_app_tail(monkeypatch, app, settings)
    monkeypatch.setattr(lifecycle, "_setup_channels", AsyncMock())
    monkeypatch.setattr(lifecycle, "_reconcile_state", AsyncMock(return_value={}))
    monkeypatch.setattr(lifecycle, "_activate_runtime_owners", AsyncMock())
    monkeypatch.setattr(lifecycle, "_publish_runtime_owners", AsyncMock())
    monkeypatch.setattr(lifecycle.tunnel_plugins, "check_tunnels", MagicMock())
    monkeypatch.setattr(lifecycle.status, "record_start_time", MagicMock())
    monkeypatch.setattr(lifecycle, "resolve_control_plane_runtime", MagicMock(return_value=runtime))
    monkeypatch.setattr(
        lifecycle.dep_factory,
        "make_http_deps",
        MagicMock(return_value=http_deps),
    )
    monkeypatch.setattr(
        lifecycle.dep_factory,
        "make_status_deps",
        MagicMock(return_value=status_deps),
    )
    monkeypatch.setattr(lifecycle, "github_webhook_routes", MagicMock(return_value=("route",)))
    monkeypatch.setattr(
        lifecycle.http_server,
        "prepare_http_server",
        AsyncMock(return_value=prepared),
    )
    activate = AsyncMock()
    monkeypatch.setattr(lifecycle.http_server, "activate_http_server", activate)

    await lifecycle.run_app(app)

    activate.assert_awaited_once_with(prepared)
    lifecycle.http_server.prepare_http_server.assert_awaited_once_with(
        http_deps,
        runtime=runtime,
        status_deps=status_deps,
        github_webhook_routes=("route",),
    )


@pytest.mark.asyncio
async def test_run_app_keeps_healthy_startup_when_continuation_cleanup_is_deferred(
    monkeypatch, tmp_path
) -> None:
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy")
    app.workspaces = {"slack:C123": _workspace()}
    settings = make_settings(data_dir=tmp_path / "data")
    prepared = _prepared_http_server()
    resolve = AsyncMock()
    finalize = AsyncMock(side_effect=OSError("continuation is busy"))
    publish = MagicMock()
    start_ipc = MagicMock()
    _patch_run_app_tail(monkeypatch, app, settings)
    monkeypatch.setattr(lifecycle, "_setup_channels", AsyncMock())
    monkeypatch.setattr(lifecycle, "_reconcile_state", AsyncMock(return_value={}))
    monkeypatch.setattr(
        lifecycle,
        "_prepare_and_bind_control_plane",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(lifecycle, "_activate_runtime_owners", AsyncMock())
    monkeypatch.setattr(lifecycle.startup_handler, "resolve_deploy_startup", resolve)
    monkeypatch.setattr(lifecycle.startup_handler, "finalize_deploy_startup", finalize)
    monkeypatch.setattr(
        lifecycle,
        "current_deploy_revision",
        MagicMock(return_value=("sha", "cfg")),
    )
    monkeypatch.setattr(lifecycle.http_server, "publish_http_server", publish)
    monkeypatch.setattr(lifecycle, "_start_ipc_watcher", start_ipc)

    await lifecycle.run_app(app)

    resolve.assert_awaited_once_with(_recovery(), active_revision=("sha", "cfg"))
    finalize.assert_awaited_once_with(_recovery())
    publish.assert_called_once_with(prepared)
    start_ipc.assert_called_once_with(app)
    await app.startup_readiness.wait()


@pytest.mark.asyncio
async def test_shutdown_app_notifies_admin_and_closes_owned_resources(monkeypatch) -> None:
    app = PynchyApp()
    app.queue = cast("Any", _RecordingQueue())
    channel = MagicMock()
    channel.prepare_shutdown = MagicMock()
    channel.disconnect = AsyncMock()
    app.channels = [channel]
    app.broadcast_host_message = AsyncMock()
    app.cleanup_http_runner = AsyncMock()
    app.close_observers = AsyncMock()
    app.connection_runtime_owner.close = AsyncMock()
    batcher = MagicMock()
    batcher.flush_all = AsyncMock()
    watchdog = MagicMock()

    monkeypatch.setattr(
        lifecycle.orchestrator_adapters,
        "resolve_admin_notification_jid",
        lambda _workspaces, _admin_workspace: "admin@g.us",
    )
    monkeypatch.setattr(
        lifecycle,
        "get_settings",
        lambda: MagicMock(notifications=MagicMock(admin_workspace="admin")),
    )
    monkeypatch.setattr(lifecycle, "_start_shutdown_watchdog", lambda: watchdog)
    monkeypatch.setattr(lifecycle.output_handler, "get_trace_batcher", lambda: batcher)
    monkeypatch.setattr(lifecycle.gateway_manager, "stop_gateway", AsyncMock())
    monkeypatch.setattr(lifecycle.asyncio, "sleep", AsyncMock())

    await lifecycle.shutdown_app(app, "SIGTERM")

    app.broadcast_host_message.assert_awaited_once_with("admin@g.us", "Shutting down (SIGTERM)")
    channel.prepare_shutdown.assert_called_once_with()
    app.cleanup_http_runner.assert_awaited_once_with()
    app.close_observers.assert_awaited_once_with()
    batcher.flush_all.assert_awaited_once_with()
    app.connection_runtime_owner.close.assert_awaited_once_with()
    channel.disconnect.assert_awaited_once_with()
    watchdog.cancel.assert_called_once_with()
    assert app.queue.shutdown_called is True


@pytest.mark.asyncio
async def test_shutdown_app_force_exits_when_shutdown_already_started(monkeypatch) -> None:
    app = PynchyApp()
    app.begin_shutdown()

    class ForceExitError(Exception):
        pass

    def force_exit(code: int) -> None:
        raise ForceExitError(code)

    monkeypatch.setattr(lifecycle.os, "_exit", force_exit)

    with pytest.raises(ForceExitError) as exc_info:
        await lifecycle.shutdown_app(app, "SIGINT")

    assert exc_info.value.args == (1,)


@pytest.mark.asyncio
async def test_run_app_rolls_back_claimed_continuation_when_core_startup_fails(
    monkeypatch, tmp_path
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    continuation_path = settings.data_dir / "deploy_continuation.json"
    continuation_path.parent.mkdir()
    continuation_path.write_text("{}")
    startup_error = RuntimeError("gateway unavailable")
    rollback = AsyncMock()

    async def fail_core(_app: PynchyApp) -> None:
        await asyncio.sleep(0)
        raise startup_error

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "claim_deploy_continuation",
        lambda _: continuation_path,
    )
    monkeypatch.setattr(lifecycle, "_initialize_core", fail_core)
    monkeypatch.setattr(lifecycle.startup_handler, "auto_rollback", rollback)
    stop_gateway = AsyncMock()
    monkeypatch.setattr(
        lifecycle.gateway_manager, "stop_gateway_after_startup_failure", stop_gateway
    )

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await lifecycle.run_app(PynchyApp())

    rollback.assert_awaited_once_with(continuation_path, startup_error)
    stop_gateway.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_run_app_rolls_back_claimed_continuation_when_channels_fail(
    monkeypatch, tmp_path
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    continuation_path = settings.data_dir / "deploy_continuation.json"
    continuation_path.parent.mkdir()
    continuation_path.write_text("{}")
    startup_error = RuntimeError("channel unavailable")
    rollback = AsyncMock()
    loop = asyncio.get_running_loop()

    async def fail_channels(_app: PynchyApp) -> None:
        await asyncio.sleep(0)
        raise startup_error

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "claim_deploy_continuation",
        lambda _: continuation_path,
    )
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(lifecycle, "_setup_channels", fail_channels)
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(lifecycle.startup_handler, "auto_rollback", rollback)

    with pytest.raises(RuntimeError, match="channel unavailable"):
        await lifecycle.run_app(PynchyApp())

    rollback.assert_awaited_once_with(continuation_path, startup_error)


@pytest.mark.asyncio
async def test_run_app_skips_duplicate_message_loop_after_admin_setup(
    monkeypatch, tmp_path
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    app = PynchyApp()
    app.message_loop_running = True
    default_channel = MagicMock()
    setup_admin_group = AsyncMock()
    loop = asyncio.get_running_loop()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(lifecycle, "_setup_channels", AsyncMock())
    monkeypatch.setattr(lifecycle, "resolve_default_channel", lambda *_args: default_channel)
    monkeypatch.setattr(lifecycle.startup_handler, "setup_admin_group", setup_admin_group)
    monkeypatch.setattr(
        lifecycle,
        "_prepare_state_and_subsystems",
        AsyncMock(return_value=_recovery()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", AsyncMock())
    monkeypatch.setattr(app, "catch_up_channels", AsyncMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", AsyncMock())
    message_loop = AsyncMock()
    monkeypatch.setattr(lifecycle, "start_message_loop", message_loop)

    await lifecycle.run_app(app)

    setup_admin_group.assert_awaited_once_with(app, default_channel)
    app.catch_up_channels.assert_awaited_once_with()
    message_loop.assert_not_awaited()
