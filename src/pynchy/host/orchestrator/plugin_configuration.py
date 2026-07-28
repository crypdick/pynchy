"""Named composition for plugins that receive resolved host configuration."""

from __future__ import annotations

# allow: file-length - one composition root keeps plugin wiring and lifecycle ownership explicit.
from pathlib import Path
from typing import TYPE_CHECKING

from temporalio.service import RPCError

from pynchy.canaries import (
    register_canary_scenario,
    register_security_canary_scenario,
    registered_canary_scenarios,
)
from pynchy.canary_contracts import (
    CanaryScenario,  # noqa: TC001, RUF100 - beartype resolves registration callback annotations at runtime.
)
from pynchy.config.api import (  # noqa: TC001, RUF100 - beartype resolves composition annotations at runtime.
    CalDAVTool,
    McpTool,
    Settings,
    tool_process_environment,
)
from pynchy.conversation.api import (  # noqa: TC001, RUF100 - beartype resolves composition callback annotations at runtime.
    ConversationId,
)
from pynchy.host.container_manager import gateway as gateway_manager
from pynchy.host.container_manager.mcp.google_canaries import (
    GoogleCanaryConfig,
    register_google_canary_scenarios,
)
from pynchy.host.container_manager.security.security_canaries import (
    register_security_canary_scenarios,
)
from pynchy.host.orchestrator.api import resolve_workspace_placement, static_workspace_folder
from pynchy.host.orchestrator.temporal.workflow_control import (
    TemporalRuntimeUnavailableError,
    cancel_scheduled_agent_workflow,
)
from pynchy.identifiers import (
    ChatJid,  # noqa: TC001, RUF100 - beartype resolves composition callback annotations at runtime.
)
from pynchy.integration_contracts import (
    is_matrix_connection,  # noqa: TC001, RUF100 - beartype resolves composition callback annotations at runtime.
)
from pynchy.plugins.api import ComputerUseRouterConfig
from pynchy.plugins.integrations.api import linear_account_for_workspace
from pynchy.plugins.integrations.caldav import (
    CalDAVRuntime,
    CalDAVServerOptions,
    configure_caldav_runtime,
)
from pynchy.plugins.integrations.computer_use import ComputerUsePlugin
from pynchy.plugins.integrations.cua_driver import CuaDriverComputerUsePlugin, CuaDriverConfig
from pynchy.plugins.integrations.desktop_screenshot import (
    DesktopScreenshotPlugin,
    DesktopScreenshotRuntime,
    DesktopVisionGateway,
)
from pynchy.plugins.integrations.gog import GogConfig, GogRuntime, configure_gog_runtime
from pynchy.plugins.integrations.google_setup import (
    GoogleSetupPlugin,
    GoogleSetupRuntime,
    configure_google_setup_runtime,
)
from pynchy.plugins.integrations.linear import LinearMcpPlugin, WorkspaceContext
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,  # noqa: TC001, RUF100 - beartype resolves composition callback annotations at runtime.
    LinearAccountRuntime,
    configure_linear_account_runtime,
    configured_linear_accounts,
    linear_account,
)
from pynchy.plugins.integrations.linear_boot import (
    LinearBootRuntime,
    configure_linear_boot_runtime,
    configured_linear_workspace_names,
)
from pynchy.plugins.integrations.linear_conversation_identity import (
    LinearConversationRuntime,
    configure_linear_conversation_runtime,
)
from pynchy.plugins.integrations.linear_legacy_work_items import (
    LinearLegacyWorkItemRuntime,
    configure_linear_legacy_work_item_runtime,
)
from pynchy.plugins.integrations.linear_planning_tasks import (
    LinearPlanningTaskRuntime,
    configure_linear_planning_task_runtime,
)
from pynchy.plugins.integrations.linear_self_echoes import (
    LinearSelfEchoRuntime,
    configure_linear_self_echo_runtime,
)
from pynchy.plugins.integrations.linear_session_reset import LinearSessionResetState
from pynchy.plugins.integrations.linear_webhook_config import LinearPluginOptions
from pynchy.plugins.integrations.linear_webhook_effects import (
    LinearWebhookEffectsRuntime,
    configure_linear_webhook_effects_runtime,
)
from pynchy.plugins.integrations.linear_webhooks import (
    LinearWebhookRuntime,
    configure_linear_webhook_runtime,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    LinearWorkItemCompletionRuntime,
    configure_linear_work_item_completion_runtime,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkItemRuntime,
    configure_linear_work_item_runtime,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    LinearWorkItemTaskRuntime,
    configure_linear_work_item_task_runtime,
)
from pynchy.plugins.integrations.linear_work_items import (
    LinearWorkItemsRuntime,
    configure_linear_work_items_runtime,
)
from pynchy.plugins.integrations.marketplace_health import (
    MarketplaceHealthOptions,
    MarketplaceHealthPlugin,
    MarketplaceHealthRuntime,
    configure_marketplace_health_runtime,
)
from pynchy.plugins.integrations.matrix_gateway import (
    MatrixConnectionRuntimeOptions,
    MatrixGatewayRuntime,
    configure_matrix_gateway_runtime,
)
from pynchy.plugins.integrations.matrix_route_resolution import (
    MatrixRouteInput,
    MatrixWorkspacePolicy,
    resolve_matrix_routes,
)
from pynchy.plugins.integrations.operational_canaries import (
    CalendarRoundTripCanary,
    LinearWorkspaceRoundTripCanary,
    ProtonMailRoundTripCanary,
    linear_client_context,
    proton_client_factory,
    register_operational_canary_scenarios,
)
from pynchy.plugins.integrations.peekaboo import PeekabooComputerUsePlugin, PeekabooConfig
from pynchy.plugins.observers.sqlite_observer import SqliteObserverPlugin
from pynchy.state.api import (
    WorkItemClaimRequest,
    WorkItemTransitionRequest,
    WorkItemTransitionResolution,
    apply_conversation_control_state,
    begin_webhook_effect,
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
    bind_work_item_execution_to_task,
    bind_work_item_execution_to_turn,
    cancel_task_and_checkpoint,
    cancel_work_item_execution,
    cancel_work_item_execution_if_lifecycle_current,
    confirm_webhook_effect,
    conversation_control_state_matches,
    create_task_if_absent,
    create_work_item_claim,
    fail_webhook_effect,
    get_active_work_item_execution,
    get_all_tasks,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    get_conversation_for_subject_key,
    get_in_flight_turn_for_group,
    get_latest_unresolved_work_item_transition,
    get_task_by_id,
    get_task_run_logs,
    get_unfinished_work_item_execution,
    get_work_item_execution,
    get_work_item_execution_for_issue,
    get_work_item_transition_by_request,
    list_work_item_executions,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
    resolve_conversation,
    resolve_work_item_transition,
    resolve_work_item_transition_if_lifecycle_current,
    resume_once_task_after_unclaimed_scheduled_turn,
    store_event,
    update_task,
)
from pynchy.utils import filtered_process_environment
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001, RUF100 - beartype resolves composition callback annotations at runtime.
)

if TYPE_CHECKING:
    import pluggy


def configure_computer_use_plugins(
    plugin_manager: pluggy.PluginManager,
    settings: Settings,
) -> None:
    """Inject the router and provider options resolved at composition."""
    router = plugin_manager.get_plugin("builtin-computer-use")
    if isinstance(router, ComputerUsePlugin):
        router.configure(
            ComputerUseRouterConfig.model_validate(
                getattr(settings.plugins.get("computer-use"), "options", {})
            ),
            data_dir=settings.data_dir,
        )
    cua = plugin_manager.get_plugin("builtin-cua-driver")
    if isinstance(cua, CuaDriverComputerUsePlugin):
        cua.configure(
            CuaDriverConfig.model_validate(
                getattr(settings.plugins.get("cua-driver"), "options", {})
            )
        )
    peekaboo = plugin_manager.get_plugin("builtin-peekaboo")
    if isinstance(peekaboo, PeekabooComputerUsePlugin):
        peekaboo.configure(
            PeekabooConfig.model_validate(getattr(settings.plugins.get("peekaboo"), "options", {}))
        )


def configure_desktop_screenshot_plugin(
    plugin_manager: pluggy.PluginManager,
    settings: Settings,
) -> None:
    """Inject the host data directory and local vision gateway accessor."""
    plugin = plugin_manager.get_plugin("builtin-desktop-screenshot")
    if not isinstance(plugin, DesktopScreenshotPlugin):
        return

    def vision_gateway() -> DesktopVisionGateway | None:
        gateway = gateway_manager.get_gateway()
        if gateway is None:
            return None
        return DesktopVisionGateway(port=gateway.port, api_key=gateway.key)

    plugin.configure(
        DesktopScreenshotRuntime(
            data_dir=settings.data_dir,
            default_model=settings.agent.model or "gpt-5.5",
            vision_gateway=vision_gateway,
        )
    )


def configure_caldav_plugin(settings: Settings) -> None:
    """Inject CalDAV connection settings resolved by the host composition root."""
    tool = settings.tools.get("caldav")
    if not isinstance(tool, CalDAVTool):
        configure_caldav_runtime(CalDAVRuntime(default_server="", servers={}))
        return
    configure_caldav_runtime(
        CalDAVRuntime(
            default_server=tool.default_server,
            servers={
                name: CalDAVServerOptions(
                    url=server.url,
                    username=server.username,
                    password_env=server.password_env,
                    default_calendar=server.default_calendar,
                    allow=tuple(server.allow) if server.allow is not None else None,
                    ignore=tuple(server.ignore) if server.ignore is not None else None,
                )
                for name, server in tool.servers.items()
            },
        )
    )


def configure_gog_plugin(settings: Settings) -> None:
    """Inject Gog's resolved paths and enabled-workspace policy."""
    plugin = settings.plugins.get("gog")
    config = GogConfig.model_validate(plugin.options if plugin is not None else {})

    def resolve_path(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else settings.project_root / path

    def workspace_enables_gog(workspace: str) -> bool:
        try:
            resolved = settings.resolved_workspace_config(workspace)
        except ValueError:
            return False
        return resolved is not None and "gog" in resolved.tools

    configure_gog_runtime(
        GogRuntime(
            config=config,
            home=(
                resolve_path(config.home) if config.home is not None else settings.data_dir / "gog"
            ),
            oauth_client_path=(
                resolve_path(config.oauth_client_path)
                if config.oauth_client_path is not None
                else None
            ),
            workspace_enables_gog=workspace_enables_gog,
        )
    )


def configure_linear_plugin(plugin_manager: pluggy.PluginManager, settings: Settings) -> None:
    """Inject the named Linear accounts resolved at application composition."""
    plugin = settings.plugins.get("linear")

    def workspace_tools(workspace: str) -> tuple[str, ...] | None:
        resolved = settings.resolved_workspace_config(workspace)
        return tuple(resolved.tools) if resolved is not None else None

    def account_for_workspace(workspace: str) -> LinearAccount | None:
        return linear_account_for_workspace(
            workspace,
            tools=settings.tools,
            workspace_tool_names=workspace_tools,
        )

    def additional_workspaces(
        registered: list[WorkspaceProfile],
    ) -> tuple[WorkspaceProfile, ...]:
        return tuple(
            placement.owner
            for folder in settings.workspace_names()
            if (placement := resolve_workspace_placement(registered, folder)) is not None
        )

    configure_linear_boot_runtime(
        LinearBootRuntime(
            workspace_names=tuple(settings.workspace_names()),
            account_for_name=lambda name: linear_account(name, settings.tools),
            account_for_workspace=account_for_workspace,
            workspace_parent=settings.workspace_parent,
            canonical_workspace_folder=static_workspace_folder,
            additional_workspaces=additional_workspaces,
        )
    )

    configure_linear_webhook_runtime(
        LinearWebhookRuntime(
            options=LinearPluginOptions.model_validate(
                plugin.options if plugin is not None else {}
            ),
            account_for_name=lambda name: linear_account(name, settings.tools),
            workspace_tools=workspace_tools,
            workspace_names_for_account=configured_linear_workspace_names,
        )
    )
    configure_linear_webhook_effects_runtime(
        LinearWebhookEffectsRuntime(
            resolve_conversation=resolve_conversation,
            control_state_matches=conversation_control_state_matches,
            apply_control_state=apply_conversation_control_state,
            get_execution_for_issue=get_work_item_execution_for_issue,
            cancel_execution=cancel_work_item_execution,
            cancel_execution_if_lifecycle_current=cancel_work_item_execution_if_lifecycle_current,
            get_active_execution=get_active_work_item_execution,
        )
    )
    configure_linear_work_item_task_runtime(
        LinearWorkItemTaskRuntime(
            get_control_binding=get_conversation_control_binding,
            get_task=get_task_by_id,
            create_task=create_task_if_absent,
            update_task=update_task,
            get_task_logs=get_task_run_logs,
            bind_execution_to_task=bind_work_item_execution_to_task,
            get_active_execution=get_active_work_item_execution,
            resume_once_task=resume_once_task_after_unclaimed_scheduled_turn,
            get_execution_for_issue=get_work_item_execution_for_issue,
        )
    )
    configure_linear_work_items_runtime(
        LinearWorkItemsRuntime(
            list_executions=list_work_item_executions,
            get_active_execution=get_active_work_item_execution,
            get_execution_for_issue=get_work_item_execution_for_issue,
            get_in_flight_turn=get_in_flight_turn_for_group,
            bind_execution_to_turn=bind_work_item_execution_to_turn,
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
        )
    )
    configure_linear_work_item_runtime(
        LinearWorkItemRuntime(
            get_transition_by_request=get_work_item_transition_by_request,
            get_execution=get_work_item_execution,
            get_active_execution=get_active_work_item_execution,
            create_claim=create_work_item_claim,
            claim_request=WorkItemClaimRequest,
            begin_transition=begin_work_item_transition,
            transition_resolution=WorkItemTransitionResolution,
            resolve_transition=resolve_work_item_transition,
            resolve_transition_if_lifecycle_current=resolve_work_item_transition_if_lifecycle_current,
        )
    )
    configure_linear_work_item_completion_runtime(
        LinearWorkItemCompletionRuntime(
            get_execution_for_issue=get_work_item_execution_for_issue,
            get_transition_by_request=get_work_item_transition_by_request,
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
            transition_request=WorkItemTransitionRequest,
            begin_transition=begin_work_item_transition,
            begin_transition_if_lifecycle_current=begin_work_item_transition_if_lifecycle_current,
        )
    )
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=get_all_tasks,
            get_transition_by_request=get_work_item_transition_by_request,
            create_claim=create_work_item_claim,
            claim_request=WorkItemClaimRequest,
            get_active_execution=get_active_work_item_execution,
            get_execution=get_work_item_execution,
            resolve_transition=resolve_work_item_transition,
        )
    )
    configure_linear_planning_task_runtime(LinearPlanningTaskRuntime(get_all_tasks=get_all_tasks))
    configure_linear_self_echo_runtime(
        LinearSelfEchoRuntime(
            begin=begin_webhook_effect,
            mark_executing=mark_webhook_effect_executing,
            confirm=confirm_webhook_effect,
            fail=fail_webhook_effect,
            mark_outcome_unknown=mark_webhook_effect_outcome_unknown,
        )
    )
    configure_linear_conversation_runtime(
        LinearConversationRuntime(
            get_unfinished_execution=get_unfinished_work_item_execution,
            get_for_subject_key=lambda key, workspace, suffix: get_conversation_for_subject_key(
                key,
                workspace=workspace,
                namespace_suffix=suffix,
            ),
            resolve=resolve_conversation,
        )
    )

    configure_linear_account_runtime(
        LinearAccountRuntime(
            tools=settings.tools,
            workspace_tool_names=workspace_tools,
        )
    )
    linear = plugin_manager.get_plugin("builtin-linear")
    if isinstance(linear, LinearMcpPlugin):

        async def cancel_workflow(workflow_id: str) -> bool:
            try:
                return await cancel_scheduled_agent_workflow(workflow_id)
            except (RPCError, TemporalRuntimeUnavailableError):
                return False

        linear.configure(
            configured_linear_accounts(),
            cancel_scheduled_workflow=cancel_workflow,
            session_reset_state=LinearSessionResetState(
                get_control_by_thread=get_conversation_control_by_thread,
                get_conversation=get_conversation,
                get_active_execution=get_active_work_item_execution,
                cancel_task=cancel_task_and_checkpoint,
                cancel_execution=cancel_work_item_execution,
                transition_request=WorkItemTransitionRequest,
            ),
        )


def configure_observer_plugins(plugin_manager: pluggy.PluginManager) -> None:
    """Inject durable event persistence into the built-in SQLite observer."""
    observer = plugin_manager.get_plugin("sqlite-observer")
    if isinstance(observer, SqliteObserverPlugin):
        observer.configure(store_event=store_event)


def configure_marketplace_health_plugin(
    plugin_manager: pluggy.PluginManager,
    settings: Settings,
) -> None:
    """Inject marketplace projection options and the selected reader environment."""
    plugin_config = settings.plugins.get("marketplace-health")

    def reader_environment(tool_name: str) -> dict[str, str] | None:
        tool = settings.tools.get(tool_name)
        if not isinstance(tool, McpTool):
            return None
        return filtered_process_environment({**tool.mcp.env, **tool_process_environment(tool)})

    runtime = MarketplaceHealthRuntime(
        options=MarketplaceHealthOptions.model_validate(
            plugin_config.options if plugin_config is not None else {}
        ),
        reader_environment=reader_environment,
    )
    configure_marketplace_health_runtime(runtime)
    plugin = plugin_manager.get_plugin("marketplace-health")
    if isinstance(plugin, MarketplaceHealthPlugin):
        plugin.configure(runtime)


def configure_matrix_gateway_plugin(settings: Settings) -> None:
    """Inject Matrix routes and state paths selected by host configuration."""
    routes = tuple(
        MatrixRouteInput(
            name=name,
            source=route.source,
            workspace=str(route.workspace),
            activation=route.activation,
            outbound=route.outbound,
            tools=tuple(route.tools) if route.tools is not None else None,
            capabilities=dict(route.capabilities),
        )
        for name, route in settings.routes.items()
    )
    connections = {
        name: connection
        for name, connection in settings.connections.items()
        if is_matrix_connection(connection)
    }

    def workspace_policy(workspace: str) -> MatrixWorkspacePolicy | None:
        resolved = settings.resolved_workspace_config(workspace)
        if resolved is None:
            return None
        return MatrixWorkspacePolicy(
            is_admin=resolved.is_admin,
            tools=tuple(resolved.tools),
            capabilities=dict(resolved.capabilities),
        )

    configure_matrix_gateway_runtime(
        MatrixGatewayRuntime(
            data_dir=settings.data_dir,
            routes=resolve_matrix_routes(routes, connections, workspace_policy),
            connections=tuple(
                MatrixConnectionRuntimeOptions(
                    name=name,
                    poll_interval_seconds=connection.poll_interval_seconds,
                )
                for name, connection in settings.connections.items()
                if is_matrix_connection(connection)
            ),
            get_control_thread_jid=_matrix_control_thread_jid,
        )
    )


async def _matrix_control_thread_jid(conversation_id: ConversationId) -> ChatJid | None:
    binding = await get_conversation_control_binding(conversation_id)
    return binding.thread_jid if binding is not None else None


def configure_google_setup_plugin(plugin_manager: pluggy.PluginManager, settings: Settings) -> None:
    """Inject the browser profiles that own Google setup actions."""
    configure_google_setup_runtime(
        GoogleSetupRuntime(
            data_dir=settings.data_dir,
            chrome_profiles=frozenset(settings.chrome_profiles),
            workspace_names=tuple(settings.workspace_names()),
            workspace_tools=lambda workspace: (
                tuple(resolved.tools)
                if (resolved := settings.resolved_workspace_config(workspace)) is not None
                else None
            ),
            workspace_is_admin=lambda workspace: bool(
                (configured := settings.workspaces.get(workspace)) and configured.is_admin
            ),
            mcp_tool_names=frozenset(
                name for name, tool in settings.tools.items() if tool.type == "mcp"
            ),
        )
    )
    plugin = plugin_manager.get_plugin("builtin-google-setup")
    if isinstance(plugin, GoogleSetupPlugin):
        plugin.configure(tuple(settings.chrome_profiles))


def configure_builtin_canaries(settings: Settings) -> None:
    """Register concrete assurance checks from the named composition root."""
    registered = set(registered_canary_scenarios())

    def register(scenario_id: str, scenario: CanaryScenario) -> None:
        if scenario_id in registered:
            return
        register_canary_scenario(scenario_id, scenario)
        registered.add(scenario_id)

    def register_security(scenario_id: str, scenario: CanaryScenario) -> None:
        if scenario_id in registered:
            return
        register_security_canary_scenario(scenario_id, scenario)
        registered.add(scenario_id)

    canary = settings.canary
    proton_tool = settings.tools.get("proton-mail")
    proton_environment = (
        filtered_process_environment(
            {**proton_tool.mcp.env, **tool_process_environment(proton_tool)}
        )
        if isinstance(proton_tool, McpTool)
        else None
    )
    register_operational_canary_scenarios(
        register,
        calendar=CalendarRoundTripCanary(canary.calendar_name),
        linear=LinearWorkspaceRoundTripCanary(
            canary.linear_team_key,
            WorkspaceContext(
                folder=canary.linear_workspace,
                name=canary.linear_workspace.replace("-", " ").replace("_", " ").title(),
            ),
            client_context=linear_client_context(
                linear_account_for_workspace(canary.linear_workspace)
            ),
        ),
        proton=ProtonMailRoundTripCanary(
            canary.proton_mailbox,
            canary.proton_recipient,
            client_factory=proton_client_factory(proton_environment),
        ),
    )
    register_google_canary_scenarios(
        register,
        config=GoogleCanaryConfig(
            calendar_server=canary.google_calendar_server,
            calendar_id=canary.google_calendar_id,
            drive_server=canary.google_drive_server,
            drive_probe_query=canary.google_drive_probe_query,
            drive_file_id=canary.google_drive_file_id,
        ),
    )
    register_security_canary_scenarios(register_security)
