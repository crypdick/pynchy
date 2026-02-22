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
from typing import Any, ClassVar, Literal

from croniter import croniter
from pydantic import BaseModel, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pynchy.config_mcp import McpServerConfig

# ---------------------------------------------------------------------------
# Sub-models (each maps to a [section] in config.toml)
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Base for all config sub-models — reject unknown keys so typos fail loudly."""

    model_config = {"extra": "forbid"}


class AgentConfig(_StrictModel):
    name: str = "pynchy"
    # NOTE: Update docs/architecture/message-routing.md § Trigger Pattern if you change this
    trigger_aliases: list[str] = ["ghost"]
    core: str = "claude"  # "claude" or "openai"


class ContainerConfig(_StrictModel):
    image: str = "pynchy-agent:latest"
    timeout_ms: int = 1800000  # 30 minutes
    max_output_size: int = 10485760  # 10MB
    idle_timeout_ms: int = 1800000  # 30 minutes
    max_concurrent: int = 10
    runtime: str | None = None  # "docker" | plugin runtime name (e.g. "apple") | None

    @field_validator("max_concurrent")
    @classmethod
    def clamp_max_concurrent(cls, v: int) -> int:
        return max(1, v)


class ServerConfig(_StrictModel):
    port: int = 8484


class LoggingConfig(_StrictModel):
    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def normalize_level(cls, v: str) -> str:
        return v.upper()


# NOTE: Update docs/architecture/security.md § Credential Filtering if you change these fields
class SecretsConfig(_StrictModel):
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gh_token: SecretStr | None = None
    claude_code_oauth_token: SecretStr | None = None


class GatewayConfig(_StrictModel):
    """LLM API gateway — credential isolation for containers.

    Two modes:

    **LiteLLM** (``litellm_config`` is set): Runs a LiteLLM proxy Docker
    container.  All LLM routing config (models, keys, budgets, load
    balancing) lives in the litellm YAML — no translation needed.

    **Builtin** (``litellm_config`` is ``None``): Simple aiohttp reverse
    proxy for single-key setups.  Uses keys from ``[secrets]``.
    """

    port: int = 4010  # set to 4000 when using litellm mode
    host: str = "0.0.0.0"  # bind address
    container_host: str = "host.docker.internal"  # hostname containers use to reach host
    litellm_config: str | None = None  # path to litellm_config.yaml; None = builtin mode
    litellm_image: str = "ghcr.io/berriai/litellm:main-latest"
    postgres_image: str = "postgres:17-alpine"
    master_key: SecretStr | None = None  # LiteLLM master key (required for LiteLLM mode)
    ui_username: str | None = None  # LiteLLM UI login username
    ui_password: SecretStr | None = None  # LiteLLM UI login password


class OwnerConfig(_StrictModel):
    """Owner identity per platform — used for allowed_users = ["owner"] resolution."""

    slack: str | None = None
    # WhatsApp uses is_from_me, no config needed


class ChannelOverrideConfig(_StrictModel):
    """Per-channel config override — None fields inherit from workspace/defaults."""

    access: Literal["read", "write", "readwrite"] | None = None
    mode: Literal["agent", "chat"] | None = None
    trust: bool | None = None
    trigger: Literal["mention", "always"] | None = None
    allowed_users: list[str] | None = None


class WorkspaceDefaultsConfig(_StrictModel):
    context_mode: Literal["group", "isolated"] = "group"
    access: Literal["read", "write", "readwrite"] = "readwrite"
    mode: Literal["agent", "chat"] = "agent"
    trust: bool = True
    trigger: Literal["mention", "always"] = "mention"
    allowed_users: list[str] | None = None


class McpToolSecurityConfig(_StrictModel):
    """Per-tool security config in config.toml."""

    risk_tier: Literal["always-approve", "rules-engine", "human-approval"] = "human-approval"
    enabled: bool = True


class RateLimitsConfig(_StrictModel):
    """Rate limiting config in config.toml."""

    max_calls_per_hour: int = 500
    per_tool_overrides: dict[str, int] = {}

    @field_validator("max_calls_per_hour")
    @classmethod
    def validate_max_calls(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_calls_per_hour must be a positive integer")
        return v


class WorkspaceSecurityConfig(_StrictModel):
    """Security profile in config.toml.

    Configures per-workspace MCP tool access control and rate limiting.
    Tools not listed in mcp_tools use the default_risk_tier.
    """

    mcp_tools: dict[str, McpToolSecurityConfig] = {}
    default_risk_tier: Literal["always-approve", "rules-engine", "human-approval"] = (
        "human-approval"
    )
    rate_limits: RateLimitsConfig | None = None


class RepoConfig(_StrictModel):
    """Config for a single tracked git repo under [repos."owner/repo"]."""

    path: str | None = (
        None  # relative to project root or absolute; None = auto-clone to data/repos/
    )
    token: SecretStr | None = None  # repo-scoped GitHub token (fine-grained PAT)

    @field_validator("path")
    @classmethod
    def resolve_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        p = Path(v)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        return str(p)


class WorkspaceConfig(_StrictModel):
    name: str  # display name — required, no silent defaults
    is_admin: bool = False
    repo_access: str | None = None  # GitHub slug (owner/repo) from [repos.*]; None = no worktree
    schedule: str | None = None  # cron expression
    prompt: str | None = None  # prompt for scheduled tasks
    context_mode: str | None = None  # None → use workspace_defaults
    security: WorkspaceSecurityConfig | None = None  # MCP tool access control
    skills: list[str] | None = None  # tier names and/or skill names; None = all
    mcp_servers: list[str] | None = None  # server names + group names, set-unioned
    mcp: dict[str, dict[str, Any]] = {}  # {server_name: {key: value}} → per-MCP kwargs
    # Channel access modes (None → inherit from workspace_defaults)
    access: Literal["read", "write", "readwrite"] | None = None
    mode: Literal["agent", "chat"] | None = None
    trust: bool | None = None
    trigger: Literal["mention", "always"] | None = None
    allowed_users: list[str] | None = None
    channels: dict[str, ChannelOverrideConfig] | None = None
    git_policy: Literal["merge-to-main", "pull-request"] | None = None  # None → merge-to-main
    idle_terminate: bool = True  # False → container stays alive until hard timeout

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, v: str | None) -> str | None:
        if v is not None and not croniter.is_valid(v):
            msg = f"Invalid cron expression: {v}"
            raise ValueError(msg)
        return v

    @property
    def is_periodic(self) -> bool:
        return self.schedule is not None and self.prompt is not None


class _ResetWords(_StrictModel):
    verbs: list[str] = ["reset", "restart", "clear", "new", "wipe"]
    nouns: list[str] = ["context", "session", "chat", "conversation"]
    aliases: list[str] = ["boom", "c"]


class _EndSessionWords(_StrictModel):
    verbs: list[str] = ["end", "stop", "close", "finish"]
    nouns: list[str] = ["session"]
    aliases: list[str] = ["done", "bye", "goodbye", "cya"]


class _RedeployWords(_StrictModel):
    aliases: list[str] = ["r"]
    verbs: list[str] = ["redeploy", "deploy"]


class CommandWordsConfig(_StrictModel):
    reset: _ResetWords = _ResetWords()
    end_session: _EndSessionWords = _EndSessionWords()
    redeploy: _RedeployWords = _RedeployWords()


class SchedulerConfig(_StrictModel):
    poll_interval: float = 60.0  # seconds
    timezone: str = ""  # empty → auto-detect


class CronJobConfig(_StrictModel):
    schedule: str  # cron expression
    command: str
    cwd: str | None = None  # optional working directory (relative to project root or absolute)
    timeout_seconds: int = 600
    enabled: bool = True

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, v: str) -> str:
        if not croniter.is_valid(v):
            msg = f"Invalid cron expression: {v}"
            raise ValueError(msg)
        return v

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        command = v.strip()
        if not command:
            raise ValueError("Cron job command cannot be empty")
        return command

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timeout_seconds must be positive")
        return v


class IntervalsConfig(_StrictModel):
    message_poll: float = 2.0  # seconds
    ipc_poll: float = 1.0  # seconds


class QueueConfig(_StrictModel):
    max_retries: int = 5
    base_retry_seconds: float = 5.0


class ChannelsConfig(_StrictModel):
    command_center: str | None = None


class PluginConfig(_StrictModel):
    enabled: bool = True


# TODO: move this when we split out the slack plugin to its own repo.
class SlackConfig(_StrictModel):
    bot_token: SecretStr | None = None  # xoxb-... Bot User OAuth Token
    app_token: SecretStr | None = None  # xapp-... App-Level Token (Socket Mode)


class CalDAVServerConfig(_StrictModel):
    url: str
    username: str
    password: SecretStr | None = None
    default_calendar: str | None = None  # what "primary" resolves to; None → first discovered
    allow: list[str] | None = None  # only expose these calendars (case-insensitive)
    ignore: list[str] | None = None  # hide these (case-insensitive; ignored if allow set)


class CalDAVConfig(_StrictModel):
    default_server: str = ""  # which server to use when no server prefix given
    servers: dict[str, CalDAVServerConfig] = {}


class DirectiveConfig(_StrictModel):
    """A system prompt directive scoped to specific workspaces.

    Scope values:
    - "all" → matches every workspace
    - Contains "/" → repo slug, matches workspaces whose repo_access equals it
    - Otherwise → workspace folder name
    - Can be a string or list (union of scopes)
    - None (omitted) → never matches (logged as warning)
    """

    file: str
    scope: str | list[str] | None = None


class SecurityConfig(_StrictModel):
    blocked_patterns: list[str] = [
        ".ssh",
        ".gnupg",
        ".gpg",
        ".aws",
        ".azure",
        ".gcloud",
        ".kube",
        ".docker",
        "credentials",
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "private_key",
        ".secret",
    ]


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
    repos: dict[str, RepoConfig] = {}  # [repos."owner/repo"]
    workspaces: dict[str, WorkspaceConfig] = {}  # [workspaces.<folder_name>]
    owner: OwnerConfig = OwnerConfig()
    user_groups: dict[str, list[str]] = {}  # group_name → [user IDs or group refs]
    commands: CommandWordsConfig = CommandWordsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    cron_jobs: dict[str, CronJobConfig] = {}  # [cron_jobs.<job_name>]
    intervals: IntervalsConfig = IntervalsConfig()
    queue: QueueConfig = QueueConfig()
    channels: ChannelsConfig = ChannelsConfig()
    plugins: dict[str, PluginConfig] = {}
    directives: dict[str, DirectiveConfig] = {}
    security: SecurityConfig = SecurityConfig()
    slack: SlackConfig = SlackConfig()
    caldav: CalDAVConfig = CalDAVConfig()

    # MCP management (imported from config_mcp)
    mcp_servers: dict[str, McpServerConfig] = {}  # [mcp_servers.<name>]
    mcp_groups: dict[str, list[str]] = {}  # {group_name: [server_names]}
    mcp_presets: dict[str, dict[str, str]] = {}  # {preset_name: {key: value}}

    # Sentinels (class-level, not fields)
    OUTPUT_START_MARKER: ClassVar[str] = "---PYNCHY_OUTPUT_START---"
    OUTPUT_END_MARKER: ClassVar[str] = "---PYNCHY_OUTPUT_END---"

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
    """Programmatically add a workspace to config.toml using tomlkit.

    Preserves existing comments and formatting. Creates [workspaces.<folder>]
    section. Resets the settings cache so next get_settings() picks it up.
    """
    import tomlkit

    toml_path = Path("config.toml")
    doc = tomlkit.parse(toml_path.read_text()) if toml_path.exists() else tomlkit.document()

    if "workspaces" not in doc:
        doc.add("workspaces", tomlkit.table(is_super_table=True))

    ws_table = tomlkit.table()
    data = config.model_dump(exclude_none=True, exclude_defaults=True)
    for key, value in data.items():
        ws_table.add(key, value)

    doc["workspaces"][folder] = ws_table  # type: ignore[index]

    toml_path.write_text(tomlkit.dumps(doc))

    # Reset so next get_settings() re-reads the file
    reset_settings()
