"""Centralized Pydantic settings from TOML, dotenv, and environment variables."""

from __future__ import annotations

# allow: file-length - one Pydantic settings model must retain field and validator order.
import os
import re
import warnings
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.
    Iterable,
    Sequence,
)
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pynchy.config import settings_validation
from pynchy.config.jobs import (
    JobConfig,  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
)
from pynchy.config.merge import ResolvedWorkspaceConfig, merge_workspace_profiles
from pynchy.config.models import (
    AgentConfig,
    CommandCenterConfig,
    ConnectionConfig,
    ContainerConfig,
    GatewayConfig,
    LearningConfig,
    LoggingConfig,
    McpTool,
    McpToolConfig,
    NotificationsConfig,
    PluginConfig,
    ReposConfig,
    RouteConfig,
    SecretsConfig,
    SecurityConfig,
    ToolConfig,
    WorkspaceConfig,
)
from pynchy.config.profiles import (
    ProfileConfig,  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
)
from pynchy.config.scheduler_models import (
    CanaryConfig,
    CommandWordsConfig,
    CronJobConfig,
    IntervalsConfig,
    QueueConfig,
    SchedulerConfig,
)
from pynchy.config.server import ServerConfig
from pynchy.config.settings_sources import (
    FilteredDotenvSettingsSource,
    hermetic_settings_sources,
    hermetic_settings_sources_enabled,
)
from pynchy.config.workspace_layout import (
    WorkspaceMigrationConfig,
    semantic_workspace_configs,
    validate_workspace_migrations,
)
from pynchy.config.workspace_names import static_workspace_name


def _assert_admin_clean_room(
    settings: Settings, *, workspace_name: str, workspace: WorkspaceConfig
) -> None:
    _ = workspace  # Preserve the keyword-only call contract for beartype.
    resolved = settings.resolved_workspace_config(workspace_name)
    if resolved is None:
        return
    for tool_name in resolved.tools:
        tool = settings.tools[tool_name]
        if tool.public_source is not False:
            message = (
                f"Admin workspace '{workspace_name}' has tool '{tool_name}' "
                f"with public_source={tool.public_source!r}. Admin workspaces "
                "cannot use public-source tools."
            )
            raise ValueError(message)


def _validated_command_center_connection(settings: Settings) -> None:
    connection = settings.command_center.connection
    if not connection:
        return
    if connection not in settings.connections:
        message = f"command_center.connection references unknown connection: {connection}"
        raise ValueError(message)


def _validate_owner_aliases(settings: Settings) -> None:
    for connection_name, connection in settings.connections.items():
        connection_security = getattr(connection, "security", None)
        _validate_owner_alias(
            connection_name,
            connection.type,
            getattr(connection_security, "allowed_users", None)
            if connection_security is not None
            else None,
            settings,
        )
        for chat_name, chat in getattr(connection, "chat", {}).items():
            chat_security = getattr(chat, "security", None)
            _validate_owner_alias(
                f"{connection_name}.chat.{chat_name}",
                connection.type,
                getattr(chat_security, "allowed_users", None)
                if chat_security is not None
                else None,
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


# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file="config.toml",
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
    profiles: dict[str, ProfileConfig] = {}
    workspaces: dict[str, WorkspaceConfig] = Field(default_factory=dict)
    workspace_migrations: dict[str, WorkspaceMigrationConfig] = Field(default_factory=dict)
    user_groups: dict[str, list[str]] = {}  # group_name → [user IDs or group refs]
    commands: CommandWordsConfig = CommandWordsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    canary: CanaryConfig = CanaryConfig()
    cron_jobs: dict[str, CronJobConfig] = {}  # internal adapter for host [jobs.<job_name>]
    jobs: dict[str, JobConfig] = {}
    intervals: IntervalsConfig = IntervalsConfig()
    queue: QueueConfig = QueueConfig()
    command_center: CommandCenterConfig = CommandCenterConfig()
    notifications: NotificationsConfig = NotificationsConfig()
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
    def _reject_legacy_sections(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict):
            legacy = [
                k
                for k in (
                    "sandbox",
                    "universal",
                    "sandbox_universal",
                    "sandbox_profiles",
                    "services",
                    "channels",
                    "slack",
                    "workspace_defaults",
                    "directives",
                    "mcp",
                    "mcp_servers",
                    "mcp_groups",
                    "mcp_presets",
                    "mcp_server_instances",
                    "connection",
                    "owner",
                    "caldav",
                    "cron_jobs",
                    "git_policy",
                    "context_mode",
                    "idle_terminate",
                    "access",
                    "mode",
                    "trust",
                    "trigger",
                )
                if k in data
            ]
            if legacy:
                message = (
                    "Legacy config sections are no longer supported: "
                    f"{legacy}. Use [workspaces], [profiles], [tools], "
                    "[connections.*], and [command_center] instead."
                )
                raise ValueError(message)
            allowed = set(cls.model_fields)
            unknown = sorted(set(data) - allowed)
            if unknown:
                message = f"Unknown config sections are not supported: {unknown}"
                raise ValueError(message)
        return data

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
    def _derive_host_cron_jobs(self) -> Settings:
        """Adapt [jobs.*] host entries to the existing Temporal host-cron path."""
        derived = dict(self.cron_jobs)
        for job_name, job in self.jobs.items():
            if not job.is_host:
                continue
            schedule = job.schedule
            command = job.command
            if schedule is None:
                message = f"host job {job_name!r} requires schedule"
                raise ValueError(message)
            if command is None:
                message = f"host job {job_name!r} requires command"
                raise ValueError(message)
            derived[job_name] = CronJobConfig(
                enabled=job.enabled,
                schedule=schedule,
                command=command,
                cwd=job.cwd,
                timeout_seconds=job.timeout_seconds or 600,
                quiet_on_success=job.quiet_on_success or False,
            )
        self.cron_jobs = derived
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

    @model_validator(mode="after")
    def _validate_workspace_migrations(self) -> Settings:
        """Ensure each source-root retirement points at a declared child thread."""
        validate_workspace_migrations(self.workspace_migrations, self.workspaces)
        return self

    def resolved_workspace_config(self, workspace_name: str) -> ResolvedWorkspaceConfig | None:
        """Return the merged config for a workspace or its dynamic-thread parent."""
        workspace = self.workspace_config(static_workspace_name(workspace_name))
        if workspace is None:
            return None
        profile_names = self._expanded_selected_profile_names(workspace.profiles)
        resolved = merge_workspace_profiles([self.profiles[name] for name in profile_names])
        return replace(
            resolved,
            model=workspace.model if workspace.model is not None else resolved.model,
            contains_secrets=resolved.contains_secrets
            or workspace.proton_pass_env_file is not None,
        )

    def workspace_config(self, workspace_name: str) -> WorkspaceConfig | None:
        """Return a root or semantic child workspace's own policy config."""
        root = self.workspaces.get(workspace_name)
        if root is not None:
            return root
        semantic = semantic_workspace_configs(self.workspaces).get(workspace_name)
        if semantic is None:
            return None
        _parent, thread = semantic
        return WorkspaceConfig.model_validate({"profiles": thread.profiles, "model": thread.model})

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

    def mcp_tools_for_names(self, names: Iterable[str]) -> dict[str, McpToolConfig]:
        """Return strict MCP provider configs for selected MCP-backed tools."""
        result: dict[str, McpToolConfig] = {}
        for name in names:
            tool = self.tools.get(name)
            if tool is None:
                message = f"unknown tool: {name}"
                raise ValueError(message)
            if isinstance(tool, McpTool):
                result[name] = tool.mcp
        return result

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Priority: init > env vars > .env > config.toml > file secrets."""
        if hermetic_settings_sources_enabled():
            return (init_settings,)
        return (
            init_settings,
            env_settings,
            FilteredDotenvSettingsSource(dotenv_settings, settings_cls),
            TomlConfigSettingsSource(settings_cls),
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
    """Validate explicit settings data without reading env, dotenv, or config.toml."""
    with hermetic_settings_sources(), warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Config key `toml_file` is set in model_config",
            category=UserWarning,
        )
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


@dataclass
class _SettingsState:
    settings: Settings | None = None


_state = _SettingsState()


def get_settings() -> Settings:
    """Lazy cached singleton."""
    if _state.settings is None:
        _state.settings = Settings()
    return _state.settings


def reset_settings() -> None:
    """Clear the cached singleton (for tests)."""
    _state.settings = None
