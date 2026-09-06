"""Centralized Pydantic settings from TOML, dotenv, and environment variables."""

from __future__ import annotations

# allow: file-length - one Pydantic settings model must retain field and validator order.
import os
import re
import warnings
from collections.abc import (  # noqa: TC003 - beartype resolves annotations at runtime.
    Sequence,
)
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from pynchy.config import settings_validation
from pynchy.config.jobs import (
    JobConfig,  # noqa: TC001 - beartype resolves annotations at runtime.
)
from pynchy.config.merge import merge_workspace_profiles
from pynchy.config.models import (
    AgentConfig,
    BuiltinTool,
    CommandCenterConfig,
    ConnectionConfig,
    ContainerConfig,
    GatewayConfig,
    LearningConfig,
    LoggingConfig,
    McpTool,
    McpToolConfig,
    NotificationsConfig,
    OpsConfig,
    PluginConfig,
    ReposConfig,
    RouteConfig,
    SecretsConfig,
    SecurityConfig,
    ToolConfig,
    WorkspaceConfig,
    WorkspaceTool,
)
from pynchy.config.permissions import (
    PermissionConfig,  # noqa: TC001 - beartype resolves this runtime annotation.
)
from pynchy.config.profiles import (
    ProfileConfig,  # noqa: TC001 - beartype resolves annotations at runtime.
)
from pynchy.config.prompts import (
    PipelineConfig,
    PromptConfig,
)
from pynchy.config.refs import parse_chat_ref
from pynchy.config.scheduler_models import (
    CanaryConfig,
    CommandWordsConfig,
    IntervalsConfig,
    QueueConfig,
    SchedulerConfig,
)
from pynchy.config.server import ServerConfig
from pynchy.config.settings_sources import (
    FilteredDotenvSettingsSource,
    PersonalizationSettingsSource,
    hermetic_settings_sources,
    hermetic_settings_sources_enabled,
    repository_settings_sources_enabled,
)
from pynchy.config.source_health import MessagingSourceHealthConfig
from pynchy.config.workspace_layout import semantic_workspace_configs
from pynchy.config.workspace_names import static_workspace_name
from pynchy.workspace.api import (
    CapabilityRule,
    ResolvedWorkspaceConfig,
    most_restrictive_capability_rule,
)


def _assert_admin_clean_room(
    settings: Settings, *, workspace_name: str, workspace: WorkspaceConfig
) -> None:
    _ = workspace  # Preserve the keyword-only call contract for beartype.
    resolved = settings.resolved_workspace_config(workspace_name)
    if resolved is None:
        return
    for tool_name in resolved.tools:
        tool = settings.tools[tool_name]
        if isinstance(tool, WorkspaceTool):
            continue
        if tool.public_source is not False:
            message = (
                f"Admin workspace '{workspace_name}' has tool '{tool_name}' "
                f"with public_source={tool.public_source!r}. Admin workspaces "
                "cannot use public-source tools."
            )
            raise ValueError(message)


def _merge_permissions(
    capabilities: dict[str, CapabilityRule], permissions: PermissionConfig
) -> dict[str, CapabilityRule]:
    capabilities = dict(capabilities)
    for capability, decision in permissions.decisions.items():
        rule = CapabilityRule(decision=decision)
        existing = capabilities.get(capability)
        capabilities[capability] = (
            most_restrictive_capability_rule((existing, rule)) if existing else rule
        ) or rule
    return capabilities


def _validated_command_center_connection(settings: Settings) -> None:
    connection = settings.command_center.connection
    if not connection:
        return
    if connection not in settings.connections:
        message = f"command_center.connection references unknown connection: {connection}"
        raise ValueError(message)


def _validate_owner_aliases(settings: Settings) -> None:
    for connection_name, connection in settings.connections.items():
        if connection.type == "matrix":
            continue
        connection_security = connection.security
        _validate_owner_alias(
            connection_name,
            connection.type,
            connection_security.allowed_users if connection_security is not None else None,
            settings,
        )
        for chat_name, chat in connection.chat.items():
            chat_security = chat.security
            _validate_owner_alias(
                f"{connection_name}.chat.{chat_name}",
                connection.type,
                chat_security.allowed_users if chat_security is not None else None,
                settings,
            )


def _validate_owner_alias(
    scope: str,
    platform: str,
    allowed_users: list[str] | None,
    settings: Settings,
) -> None:
    del settings
    if not allowed_users or "owner" not in allowed_users:
        return
    if platform == "whatsapp":
        return
    message = f"{scope} uses allowed_users=['owner']; owner aliases are only supported for WhatsApp"
    raise ValueError(message)


def _vaultwarden_browser_tool_name(channel_name: str) -> str:
    return f"vaultwarden-browser.{channel_name}"


# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    agent: AgentConfig = AgentConfig()
    container: ContainerConfig = ContainerConfig()
    server: ServerConfig = ServerConfig()
    logging: LoggingConfig = LoggingConfig()
    secrets: SecretsConfig = SecretsConfig()
    gateway: GatewayConfig = GatewayConfig()
    learning: LearningConfig = LearningConfig()
    repos: ReposConfig = Field(default_factory=ReposConfig)
    prompts: PromptConfig = Field(default_factory=PromptConfig)
    pipelines: dict[str, PipelineConfig] = Field(default_factory=dict)
    profiles: dict[str, ProfileConfig] = {}
    workspaces: dict[str, WorkspaceConfig] = Field(default_factory=dict)
    user_groups: dict[str, list[str]] = {}  # group_name → [user IDs or group refs]
    commands: CommandWordsConfig = CommandWordsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    canary: CanaryConfig = CanaryConfig()
    jobs: dict[str, JobConfig] = {}
    intervals: IntervalsConfig = IntervalsConfig()
    queue: QueueConfig = QueueConfig()
    command_center: CommandCenterConfig = CommandCenterConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    ops: OpsConfig = OpsConfig()
    messaging_source_health: MessagingSourceHealthConfig = MessagingSourceHealthConfig()
    connections: dict[str, ConnectionConfig] = {}
    routes: dict[str, RouteConfig] = {}
    tools: dict[str, ToolConfig] = {}
    plugins: dict[str, PluginConfig] = {}
    security: SecurityConfig = SecurityConfig()

    # Chrome profiles — generic list of names; any MCP server can attach to one.
    # Each profile maps to a host directory at data/chrome-profiles/{name}/.
    chrome_profiles: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_sections(cls, data: dict[str, Any]) -> dict[str, Any]:
        allowed = set(cls.model_fields)
        unknown = sorted(set(data) - allowed)
        if unknown:
            message = f"Unknown config sections are not supported: {unknown}"
            raise ValueError(message)
        return data

    @model_validator(mode="after")
    def _configure_channel_secret_access(self) -> Settings:
        """Validate collection aliases and install the internal host-tool grant."""
        configured = self.plugins.get("vaultwarden")
        raw_collections = configured.options.get("collections", {}) if configured else {}
        collection_names = set(raw_collections) if isinstance(raw_collections, dict) else set()
        used = {
            alias
            for connection in self.connections.values()
            if connection.type == "discord"
            for guild in connection.chat.values()
            for channel in guild.channels.values()
            for alias in channel.secret_collections
        }
        unknown = sorted(used - collection_names)
        if unknown:
            raise ValueError(f"unknown Vaultwarden collection: {unknown[0]}")
        if not used:
            return self
        if configured is None or not configured.enabled:
            raise ValueError("Vaultwarden collection access requires the vaultwarden plugin")
        tool = BuiltinTool(
            type="builtin",
            public_source=False,
            secret_data=True,
            public_sink="forbidden",
            dangerous_writes="forbidden",
        )
        existing = self.tools.get("vaultwarden")
        if existing is not None and existing != tool:
            raise ValueError("tools.vaultwarden is reserved for channel-scoped secret access")
        self.tools["vaultwarden"] = tool
        admin_tool = BuiltinTool(
            type="builtin",
            public_source=False,
            secret_data=True,
            public_sink=False,
            dangerous_writes=True,
        )
        existing_admin = self.tools.get("vaultwarden-admin")
        if existing_admin is not None and existing_admin != admin_tool:
            raise ValueError(
                "tools.vaultwarden-admin is reserved for channel-scoped secret administration"
            )
        self.tools["vaultwarden-admin"] = admin_tool
        channel_names = [
            channel_name
            for connection in self.connections.values()
            if connection.type == "discord"
            for guild in connection.chat.values()
            for channel_name, channel in guild.channels.items()
            if channel.secret_collections
        ]
        duplicates = sorted(name for name in set(channel_names) if channel_names.count(name) > 1)
        if duplicates:
            raise ValueError(f"secret-enabled Discord channel name is ambiguous: {duplicates[0]}")
        for channel_name in channel_names:
            browser_name = _vaultwarden_browser_tool_name(channel_name)
            browser = McpTool(
                type="mcp",
                public_source=True,
                secret_data=True,
                public_sink=True,
                dangerous_writes=True,
                mcp=McpToolConfig(
                    runtime="script",
                    command="pynchy-vaultwarden-browser",
                    args=[
                        f"/srv/pynchy/app/data/chrome-profiles/vaultwarden-{channel_name}",
                        "--port",
                        "{port}",
                        "--host",
                        "localhost",
                    ],
                    port=9120,
                    transport="streamable_http",
                    idle_timeout=0,
                    startup_timeout_seconds=60,
                ),
            )
            existing_browser = self.tools.get(browser_name)
            if existing_browser is not None and existing_browser != browser:
                raise ValueError(f"tools.{browser_name} is reserved for channel-scoped browsing")
            self.tools[browser_name] = browser
        return self

    @model_validator(mode="after")
    def _validate_profile_refs(self) -> Settings:
        """Validate that workspace profile references exist."""
        settings_validation.validate_profile_references(
            profiles=self.profiles,
            workspaces=self.workspaces,
            jobs=self.jobs,
            tools=self.tools,
            expand_profile_names=self._expanded_profile_names,
        )
        settings_validation.validate_canary_target_profile(self, self._expanded_profile_names)
        return self

    @model_validator(mode="after")
    def _reject_claude_sdk_model_overrides(self) -> Settings:
        """Reject model settings that the built-in Claude SDK core cannot honor."""
        settings_validation.reject_claude_sdk_model_overrides(
            agent=self.agent, profiles=self.profiles, workspaces=self.workspaces
        )
        return self

    @model_validator(mode="after")
    def _validate_host_execution_workspaces(self) -> Settings:
        for workspace_name in self.workspace_names():
            resolved = self.resolved_workspace_config(workspace_name)
            if resolved is None or resolved.execution_mode != "host":
                continue
            if not resolved.is_admin:
                raise ValueError(
                    f"workspaces.{workspace_name}: execution_mode = 'host' requires is_admin = true"
                )
            if not resolved.cwd:
                raise ValueError(
                    f"workspaces.{workspace_name}: execution_mode = 'host' requires cwd"
                )
        return self

    @model_validator(mode="after")
    def _validate_connections(self) -> Settings:
        """Validate command_center.connection against [connections.<name>]."""
        _validated_command_center_connection(self)
        _validate_owner_aliases(self)
        settings_validation.validate_workspace_chat_references(self)
        settings_validation.validate_route_references(self)
        return self

    @model_validator(mode="after")
    def _validate_admin_clean_room(self) -> Settings:
        """Reject admin workspaces that resolve to public-source tools."""
        for ws_name in self.workspace_names():
            ws = self.workspace_config(ws_name)
            if ws is None:
                continue
            resolved = self.resolved_workspace_config(ws_name)
            if resolved is None or not resolved.is_admin:
                continue
            _assert_admin_clean_room(self, workspace_name=ws_name, workspace=ws)
        return self

    def resolved_workspace_config(self, workspace_name: str) -> ResolvedWorkspaceConfig | None:
        """Return the merged config for a workspace or its dynamic-thread parent."""
        workspace = self.workspace_config(static_workspace_name(workspace_name))
        if workspace is None:
            return None
        profile_names = self._expanded_selected_profile_names(workspace.profiles)
        resolved = merge_workspace_profiles([self.profiles[name] for name in profile_names])
        collections = self.secret_collections_for_workspace(workspace_name)
        access = self.secret_access_for_workspace(workspace_name)
        automatic_tools = (
            ("vaultwarden", _vaultwarden_browser_tool_name(access[0])) if access is not None else ()
        )
        tools = list(dict.fromkeys([*resolved.tools, *automatic_tools]))
        capabilities = dict(resolved.capabilities)
        for tool_name in tools:
            tool = self.tools.get(tool_name)
            if tool is not None:
                capabilities = _merge_permissions(capabilities, tool.permissions)
        capabilities = _merge_permissions(capabilities, workspace.permissions)
        return replace(
            resolved,
            tools=tools,
            contains_secrets=resolved.contains_secrets or bool(collections),
            soul=workspace.soul or self.prompts.default_soul,
            pipeline=workspace.pipeline or self.prompts.default_pipeline,
            model=workspace.model if workspace.model is not None else resolved.model,
            model_reasoning_effort=workspace.model_reasoning_effort,
            capabilities=capabilities,
        )

    def secret_access_for_workspace(
        self, workspace_name: str
    ) -> tuple[str, tuple[str, ...]] | None:
        """Return the owning Discord channel and its collection aliases."""
        static_name = static_workspace_name(workspace_name)
        root_name = self.workspace_parent(static_name) or static_name
        root = self.workspaces.get(root_name)
        ref = parse_chat_ref(root.chat if root is not None else None)
        if ref is None or ref.platform != "discord":
            return None
        connection = self.connections.get(ref.name)
        if connection is None or connection.type != "discord":
            return None
        parts = ref.chat.split(".")
        if len(parts) != 3 or parts[1] != "channels":
            return None
        guild = connection.chat.get(parts[0])
        channel = guild.channels.get(parts[2]) if guild is not None else None
        if channel is None or not channel.secret_collections:
            return None
        return parts[2], tuple(channel.secret_collections)

    def secret_collections_for_workspace(self, workspace_name: str) -> tuple[str, ...]:
        """Return collection aliases inherited from the physical Discord channel."""
        access = self.secret_access_for_workspace(workspace_name)
        return access[1] if access is not None else ()

    def workspace_config(self, workspace_name: str) -> WorkspaceConfig | None:
        """Return a root or semantic child workspace's own policy config."""
        root = self.workspaces.get(workspace_name)
        if root is not None:
            return root
        semantic = semantic_workspace_configs(self.workspaces).get(workspace_name)
        if semantic is None:
            return None
        parent, thread = semantic
        parent_config = self.workspaces[parent]
        return WorkspaceConfig.model_validate(
            {
                "profiles": thread.profiles,
                "permissions": thread.permissions,
                "soul": thread.soul or parent_config.soul,
                "pipeline": thread.pipeline or parent_config.pipeline,
                "model": thread.model,
                "model_reasoning_effort": thread.model_reasoning_effort,
            }
        )

    def workspace_parent(self, workspace_name: str) -> str | None:
        """Return the physical root that owns a semantic child workspace."""
        semantic = semantic_workspace_configs(self.workspaces).get(workspace_name)
        return semantic[0] if semantic is not None else None

    def workspace_names(self) -> tuple[str, ...]:
        """Return every routable root and semantic child workspace identity."""
        return (*self.workspaces, *semantic_workspace_configs(self.workspaces))

    def configured_agent_models(self) -> tuple[str, ...]:
        """Return the distinct model routes configured globally or per workspace."""
        models = [self.agent.model]
        models.extend(
            resolved.model
            for workspace_name in self.workspace_names()
            if (resolved := self.resolved_workspace_config(workspace_name)) is not None
        )
        return tuple(dict.fromkeys(model for model in models if model))

    def _expanded_selected_profile_names(self, profile_names: Sequence[str]) -> list[str]:
        ordered: list[str] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name not in self.profiles:
                message = f"unknown profile reference: {name}"
                raise ValueError(message)
            if name in visiting:
                cycle = " -> ".join([*visiting, name])
                message = f"profile cycle detected: {cycle}"
                raise ValueError(message)
            if name in visited:
                return
            visiting.append(name)
            for included in self.profiles[name].includes:
                visit(included)
            visiting.pop()
            visited.add(name)
            ordered.append(name)

        for name in profile_names:
            visit(name)
        return ordered

    def _expanded_profile_names(self, profile_name: str) -> list[str]:
        return self._expanded_selected_profile_names([profile_name])

    @classmethod  # noqa: V105
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Priority: init > env vars > .env > personalization > defaults > file secrets."""
        if hermetic_settings_sources_enabled():
            return (init_settings,)
        if not repository_settings_sources_enabled():
            return (
                init_settings,
                env_settings,
                FilteredDotenvSettingsSource(dotenv_settings, settings_cls),
                file_secret_settings,
            )
        return (
            init_settings,
            env_settings,
            FilteredDotenvSettingsSource(dotenv_settings, settings_cls),
            PersonalizationSettingsSource(settings_cls),
            file_secret_settings,
        )

    # --- Computed properties ---

    @cached_property
    def container_timeout(self) -> float:
        return self.container.timeout_ms / 1000

    @cached_property
    def idle_timeout(self) -> float:
        return self.container.idle_timeout_ms / 1000

    @cached_property
    def trigger_pattern(self) -> re.Pattern[str]:
        names = [re.escape(name) for name in [self.agent.name, *self.agent.trigger_aliases]]
        return re.compile(rf"^@({'|'.join(names)})\b", re.IGNORECASE)

    @cached_property
    def timezone(self) -> str:
        if self.scheduler.timezone:
            return self.scheduler.timezone
        return _detect_timezone()

    @cached_property
    def project_root(self) -> Path:
        return Path.cwd()

    @cached_property
    def home_dir(self) -> Path:
        return Path.home()

    @cached_property
    def groups_dir(self) -> Path:
        return (self.project_root / "groups").resolve()

    @cached_property
    def data_dir(self) -> Path:
        return (self.project_root / "data").resolve()

    @cached_property
    def mount_allowlist_path(self) -> Path:
        return self.home_dir / ".config" / "pynchy" / "mount-allowlist.toml"

    @cached_property
    def worktrees_dir(self) -> Path:
        """Base directory for all worktrees: data/worktrees/<owner>/<repo>/."""
        return self.data_dir / "worktrees"


def validate_settings_mapping(data: dict[str, Any]) -> Settings:
    """Validate explicit settings data without reading ambient settings sources."""
    with hermetic_settings_sources(), warnings.catch_warnings():
        return Settings(**data)


# ---------------------------------------------------------------------------
# Timezone detection (shared with logger, runs before Settings)
# ---------------------------------------------------------------------------


def _detect_timezone() -> str:
    if tz := os.environ.get("TZ"):
        return tz
    try:
        link = str(Path("/etc/localtime").readlink())
        parts = link.split("zoneinfo/")
        if len(parts) > 1:
            return parts[1]
    except OSError:
        pass  # /etc/localtime missing or not a symlink — fall back to UTC
    return "UTC"


# ---------------------------------------------------------------------------
# Singleton + TOML writer
# ---------------------------------------------------------------------------


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy cached singleton."""
    global _settings  # noqa: PLW0603 - process-wide singleton.
    if _settings is None:
        _settings = Settings()
    return _settings


def publish_settings(settings: Settings) -> None:
    """Atomically publish the validated runtime settings snapshot."""
    global _settings  # noqa: PLW0603 - process-wide singleton.
    _settings = settings


def reset_settings() -> None:
    """Clear the cached singleton (for tests)."""
    global _settings  # noqa: PLW0603 - process-wide singleton.
    _settings = None
