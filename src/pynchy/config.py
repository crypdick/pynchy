"""Centralized configuration — Pydantic BaseSettings with TOML + dotenv sources.

Non-secret settings live in config.toml. Secrets (API keys, tokens, passwords)
live in .env. Environment variables override both using ``__`` as the nested
delimiter (e.g. ``SECRETS__ANTHROPIC_API_KEY``). Secrets use SecretStr for
masking in logs.

Priority (highest wins): init args > env vars > .env > config.toml

Usage::

    from pynchy.config import get_settings

    s = get_settings()
    print(s.agent.name)
    print(s.container.image)
"""

from __future__ import annotations

import os
import re
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pynchy.config_mcp import McpServerConfig
from pynchy.config_models import (
    AgentConfig,
    CalDAVConfig,
    CommandCenterConfig,
    CommandWordsConfig,
    ConnectionsConfig,
    ContainerConfig,
    CronJobConfig,
    DirectiveConfig,
    GatewayConfig,
    IntervalsConfig,
    LoggingConfig,
    OwnerConfig,
    PluginConfig,
    QueueConfig,
    RepoConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    ServiceTrustTomlConfig,
    WorkspaceConfig,
    WorkspaceDefaultsConfig,
    _StrictModel,
)
from pynchy.config_refs import connection_ref_from_parts, parse_chat_ref, parse_connection_ref

# ---------------------------------------------------------------------------
# Explicit-fields validation
# ---------------------------------------------------------------------------


def _is_exempt_field(model_cls: type[BaseModel], field_name: str) -> bool:
    """Check if a field is exempt from the explicit-ness requirement.

    Exempt fields:
    - X | None: "inherit from parent" sentinel — TOML has no null type.
    - dict/list with empty default: container types where {} / [] means "none configured."
    """
    import types
    import typing

    field_info = model_cls.model_fields[field_name]
    annotation = field_info.annotation

    # Optional (X | None) — TOML can't express null
    if isinstance(annotation, types.UnionType) and type(None) in annotation.__args__:
        return True
    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union and type(None) in annotation.__args__:
        return True
    if annotation is type(None):
        return True

    # dict/list with empty default — forgetting {} or [] doesn't cause bugs
    return field_info.default in ([], {})


def _collect_implicit_fields(model: BaseModel, path: str) -> list[str]:
    """Recursively find _StrictModel fields that were not explicitly set.

    Only descends into sub-models that were present in the parsed input
    (via model_fields_set). This means:
    - Entire sections omitted from config.toml → not checked (known defaults).
    - Sections present in config.toml → every field must be spelled out.
    - Dict-of-models (e.g., workspaces) → each entry is checked individually.
    - Optional fields (X | None) are skipped — TOML has no null type, so these
      use None as "inherit from parent" and can't be made explicit.
    """
    errors: list[str] = []
    cls = type(model)

    for field_name in cls.model_fields:
        value = getattr(model, field_name)

        # For dict-of-models (e.g., workspaces, cron_jobs), check each entry
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, _StrictModel):
                    child_path = f"{path}.{field_name}.{key}" if path else f"{field_name}.{key}"
                    child_cls = type(item)
                    child_missing = {
                        f
                        for f in set(child_cls.model_fields) - item.model_fields_set
                        if not _is_exempt_field(child_cls, f)
                    }
                    if child_missing:
                        errors.append(f"{child_path}: missing {sorted(child_missing)}")
                    errors.extend(_collect_implicit_fields(item, child_path))
            continue

        # For direct sub-models, only check if the section was in the input
        if isinstance(value, _StrictModel) and field_name in model.model_fields_set:
            child_path = f"{path}.{field_name}" if path else field_name
            child_cls = type(value)
            child_missing = {
                f
                for f in set(child_cls.model_fields) - value.model_fields_set
                if not _is_exempt_field(child_cls, f)
            }
            if child_missing:
                errors.append(f"{child_path}: missing {sorted(child_missing)}")
            errors.extend(_collect_implicit_fields(value, child_path))

    return errors


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
    workspace_defaults: WorkspaceDefaultsConfig = WorkspaceDefaultsConfig()
    services: dict[str, ServiceTrustTomlConfig] = {}  # [services.<name>]
    repos: dict[str, RepoConfig] = {}  # [repos."owner/repo"]
    # FIXME: Rename "workspace" -> "sandbox" across config + codebase.
    workspaces: dict[str, WorkspaceConfig] = Field(
        default_factory=dict, validation_alias="sandbox"
    )  # [sandbox.<folder_name>]
    owner: OwnerConfig = OwnerConfig()
    user_groups: dict[str, list[str]] = {}  # group_name → [user IDs or group refs]
    commands: CommandWordsConfig = CommandWordsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    cron_jobs: dict[str, CronJobConfig] = {}  # [cron_jobs.<job_name>]
    intervals: IntervalsConfig = IntervalsConfig()
    queue: QueueConfig = QueueConfig()
    command_center: CommandCenterConfig = CommandCenterConfig()
    connection: ConnectionsConfig = ConnectionsConfig()
    plugins: dict[str, PluginConfig] = {}
    directives: dict[str, DirectiveConfig] = {}
    security: SecurityConfig = SecurityConfig()
    caldav: CalDAVConfig = CalDAVConfig()

    # Chrome profiles — generic list of names; any MCP server can attach to one.
    # Each profile maps to a host directory at data/chrome-profiles/{name}/.
    chrome_profiles: list[str] = []

    # MCP management (imported from config_mcp)
    mcp_servers: dict[str, McpServerConfig] = {}  # [mcp_servers.<name>]
    mcp_groups: dict[str, list[str]] = {}  # {group_name: [server_names]}
    mcp_presets: dict[str, dict[str, str]] = {}  # {preset_name: {key: value}}

    # Extracted by _separate_mcp_instances validator from nested sub-tables
    # in mcp_servers.  {template_name: {instance_name: {chrome_profile: "...", ...}}}
    mcp_server_instances: dict[str, dict[str, dict[str, Any]]] = {}

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_sections(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict):
            legacy = [k for k in ("workspaces", "channels", "slack") if k in data]
            if legacy:
                raise ValueError(
                    "Legacy config sections are no longer supported: "
                    f"{legacy}. Use [sandbox], [connection.*], and [command_center] instead."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _separate_mcp_instances(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Detect nested sub-tables in mcp_servers and separate them.

        TOML input like ``[mcp_servers.gdrive.personal]`` with
        ``chrome_profile = "personal"`` gets parsed as a nested dict under
        ``mcp_servers.gdrive``.  This validator splits those into:
        - ``mcp_servers`` — flat server definitions (base overrides)
        - ``mcp_server_instances`` — ``{template: {instance: {overrides}}}``

        A sub-key is treated as an instance (not a config field) when its
        value is a dict and the key is NOT a known McpServerConfig field.
        """
        raw = data.get("mcp_servers")
        if not isinstance(raw, dict):
            return data

        config_fields = set(McpServerConfig.model_fields)
        flat: dict[str, Any] = {}
        instanced: dict[str, dict[str, dict[str, Any]]] = {}

        for name, spec in raw.items():
            if not isinstance(spec, dict):
                flat[name] = spec
                continue

            base: dict[str, Any] = {}
            instances: dict[str, dict[str, Any]] = {}
            for k, v in spec.items():
                if isinstance(v, dict) and k not in config_fields:
                    instances[k] = v
                else:
                    base[k] = v

            if instances:
                if base:
                    flat[name] = base  # user-provided base overrides
                instanced[name] = instances
            else:
                flat[name] = spec

        data["mcp_servers"] = flat
        data["mcp_server_instances"] = instanced
        return data

    @model_validator(mode="after")
    def _require_explicit_fields(self) -> Settings:
        """Validate that all fields in config-file sections are explicitly set.

        Only checks sub-models that were actually present in the config source
        (i.e., in self.model_fields_set). Sub-models that defaulted entirely
        (section absent from config.toml) are not checked — they're using
        known-good defaults. But if you include a section, spell out every field.
        """
        errors = _collect_implicit_fields(self, "")
        if errors:
            msg = "Config fields must be explicitly set (even if null):\n"
            msg += "\n".join(f"  - {e}" for e in errors)
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_connections(self) -> Settings:
        """Validate that connection refs point to configured connections/chats.

        Uses ConnectionsConfig.get_connection() for platform-generic lookups
        so this validator doesn't need to hardcode platform names.
        """
        # Validate command_center.connection exists (if set)
        if self.command_center.connection:
            ref = parse_connection_ref(self.command_center.connection)
            if ref is None:
                raise ValueError("command_center.connection must be connection.<platform>.<name>")
            if self.connection.get_connection(ref.platform, ref.name) is None:
                raise ValueError(
                    f"command_center.connection references unknown connection: "
                    f"{connection_ref_from_parts(ref.platform, ref.name)}"
                )

        # Validate workspace chat refs point to configured connections/chats
        for folder, ws in self.workspaces.items():
            chat_ref = parse_chat_ref(ws.chat)
            if chat_ref is None:
                raise ValueError(
                    f"sandbox.{folder}.chat must be connection.<platform>.<name>.chat.<chat>"
                )
            conn = self.connection.get_connection(chat_ref.platform, chat_ref.name)
            if conn is None:
                raise ValueError(
                    f"sandbox.{folder}.chat references unknown connection: "
                    f"{connection_ref_from_parts(chat_ref.platform, chat_ref.name)}"
                )
            if chat_ref.chat not in conn.chat:
                raise ValueError(f"sandbox.{folder}.chat references unknown chat: {ws.chat}")

        return self

    @model_validator(mode="after")
    def _validate_admin_clean_room(self) -> Settings:
        """Reject admin workspaces that reference public_source MCPs.

        Admin workspaces are the most privileged — they must never be
        corruption-tainted by MCP servers that pull from public/untrusted
        sources.  An MCP not declared in ``[services]`` is treated as
        ``public_source=True`` (maximally cautious default), so it is also
        blocked.
        """
        for ws_name, ws in self.workspaces.items():
            if not ws.is_admin or not ws.mcp_servers:
                continue
            # Resolve MCP server list (expand groups, "all")
            resolved: set[str] = set()
            for entry in ws.mcp_servers:
                if entry == "all":
                    resolved.update(self.mcp_servers.keys())
                elif entry in self.mcp_groups:
                    resolved.update(self.mcp_groups[entry])
                elif entry in self.mcp_servers:
                    resolved.add(entry)
            for server_name in resolved:
                svc = self.services.get(server_name)
                public_source = svc.public_source if svc else True
                if public_source is not False:
                    raise ValueError(
                        f"Admin workspace '{ws_name}' has MCP server '{server_name}' "
                        f"with public_source={public_source!r}. Admin workspaces cannot "
                        f"have public_source MCPs (clean room policy)."
                    )
        return self

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
        return (
            init_settings,
            env_settings,
            dotenv_settings,
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
        names = [re.escape(self.agent.name)] + [
            re.escape(a.strip()) for a in self.agent.trigger_aliases
        ]
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


# ---------------------------------------------------------------------------
# Timezone detection (shared with logger, runs before Settings)
# ---------------------------------------------------------------------------


def _detect_timezone() -> str:
    if tz := os.environ.get("TZ"):
        return tz
    try:
        link = os.readlink("/etc/localtime")
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
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Clear the cached singleton (for tests)."""
    global _settings
    _settings = None


def add_workspace_to_toml(folder: str, config: WorkspaceConfig) -> None:
    """Programmatically add a sandbox to config.toml using tomlkit.

    Preserves existing comments and formatting. Creates [sandbox.<folder>]
    section. Resets the settings cache so next get_settings() picks it up.
    """
    import tomlkit

    from pynchy.logger import logger

    toml_path = Path("config.toml")
    doc = tomlkit.parse(toml_path.read_text()) if toml_path.exists() else tomlkit.document()

    if "sandbox" not in doc:
        doc.add("sandbox", tomlkit.table(is_super_table=True))

    ws_table = tomlkit.table()
    data = config.model_dump(exclude_none=True, exclude_defaults=True)
    for key, value in data.items():
        ws_table.add(key, value)

    doc["sandbox"][folder] = ws_table  # type: ignore[index]

    # Ensure the referenced chat exists under [connection.*] if possible.
    chat_ref = parse_chat_ref(config.chat)
    if chat_ref is not None:
        if "connection" not in doc:
            logger.warning("Config missing [connection] section; chat not added", chat=config.chat)
        else:
            connection_tbl = doc["connection"]
            if chat_ref.platform not in connection_tbl:
                logger.warning(
                    "Config missing connection platform; chat not added",
                    platform=chat_ref.platform,
                )
            else:
                platform_tbl = connection_tbl[chat_ref.platform]
                if chat_ref.name not in platform_tbl:
                    logger.warning(
                        "Config missing connection; chat not added",
                        connection=connection_ref_from_parts(chat_ref.platform, chat_ref.name),
                    )
                else:
                    conn_tbl = platform_tbl[chat_ref.name]
                    if "chat" not in conn_tbl:
                        conn_tbl.add("chat", tomlkit.table(is_super_table=True))
                    chat_tbl = conn_tbl["chat"]
                    if chat_ref.chat not in chat_tbl:
                        chat_tbl.add(chat_ref.chat, tomlkit.table())

    toml_path.write_text(tomlkit.dumps(doc))

    # Reset so next get_settings() re-reads the file
    reset_settings()
