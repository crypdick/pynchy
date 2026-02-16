"""Centralized configuration — Pydantic BaseSettings with TOML source.

All settings live in config.toml (optional) with env var overrides using
``__`` as the nested delimiter. Secrets use SecretStr for masking in logs.

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
from typing import ClassVar, Literal

from croniter import croniter
from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# ---------------------------------------------------------------------------
# Sub-models (each maps to a [section] in config.toml)
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    name: str = "pynchy"
    # NOTE: Update docs/architecture/message-routing.md § Trigger Pattern if you change this
    trigger_aliases: list[str] = ["ghost"]
    core: str = "claude"  # "claude" or "openai"


class ContainerConfig(BaseModel):
    image: str = "pynchy-agent:latest"
    timeout_ms: int = 1800000  # 30 minutes
    max_output_size: int = 10485760  # 10MB
    idle_timeout_ms: int = 1800000  # 30 minutes
    max_concurrent: int = 5
    runtime: str | None = None  # "docker" | plugin runtime name (e.g. "apple") | None

    @field_validator("max_concurrent")
    @classmethod
    def clamp_max_concurrent(cls, v: int) -> int:
        return max(1, v)


class ServerConfig(BaseModel):
    port: int = 8484


class LoggingConfig(BaseModel):
    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def normalize_level(cls, v: str) -> str:
        return v.upper()


# NOTE: Update docs/architecture/security.md § Credential Filtering if you change these fields
class SecretsConfig(BaseModel):
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gh_token: SecretStr | None = None
    claude_code_oauth_token: SecretStr | None = None


class GatewayConfig(BaseModel):
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


class WorkspaceDefaultsConfig(BaseModel):
    requires_trigger: bool = True
    context_mode: Literal["group", "isolated"] = "group"


class McpToolSecurityConfig(BaseModel):
    """Per-tool security config in config.toml."""

    risk_tier: Literal["always-approve", "rules-engine", "human-approval"] = "human-approval"
    enabled: bool = True


class RateLimitsConfig(BaseModel):
    """Rate limiting config in config.toml."""

    max_calls_per_hour: int = 500
    per_tool_overrides: dict[str, int] = {}

    @field_validator("max_calls_per_hour")
    @classmethod
    def validate_max_calls(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_calls_per_hour must be a positive integer")
        return v


class WorkspaceSecurityConfig(BaseModel):
    """Security profile in config.toml.

    Configures per-workspace MCP tool access control and rate limiting.
    Tools not listed in mcp_tools use the default_risk_tier.
    """

    mcp_tools: dict[str, McpToolSecurityConfig] = {}
    default_risk_tier: Literal["always-approve", "rules-engine", "human-approval"] = (
        "human-approval"
    )
    rate_limits: RateLimitsConfig | None = None


class WorkspaceConfig(BaseModel):
    is_god: bool = False
    requires_trigger: bool | None = None  # None → use workspace_defaults
    project_access: bool = False
    name: str | None = None  # display name; defaults to folder titlecased
    schedule: str | None = None  # cron expression
    prompt: str | None = None  # prompt for scheduled tasks
    context_mode: str | None = None  # None → use workspace_defaults
    security: WorkspaceSecurityConfig | None = None  # MCP tool access control

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


class _ResetWords(BaseModel):
    verbs: list[str] = ["reset", "restart", "clear", "new", "wipe"]
    nouns: list[str] = ["context", "session", "chat", "conversation"]
    aliases: list[str] = ["boom", "c"]


class _EndSessionWords(BaseModel):
    verbs: list[str] = ["end", "stop", "close", "finish"]
    nouns: list[str] = ["session"]
    aliases: list[str] = ["done", "bye", "goodbye", "cya"]


class _RedeployWords(BaseModel):
    aliases: list[str] = ["r"]
    verbs: list[str] = ["redeploy", "deploy"]


class CommandWordsConfig(BaseModel):
    reset: _ResetWords = _ResetWords()
    end_session: _EndSessionWords = _EndSessionWords()
    redeploy: _RedeployWords = _RedeployWords()


class SchedulerConfig(BaseModel):
    poll_interval: float = 60.0  # seconds
    timezone: str = ""  # empty → auto-detect


class CronJobConfig(BaseModel):
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


class IntervalsConfig(BaseModel):
    message_poll: float = 2.0  # seconds
    ipc_poll: float = 1.0  # seconds


class QueueConfig(BaseModel):
    max_retries: int = 5
    base_retry_seconds: float = 5.0


class ChannelsConfig(BaseModel):
    default: str | None = "tui"


class PluginConfig(BaseModel):
    repo: str
    ref: str = "main"
    enabled: bool = True
    trusted: bool = False  # Skip verification — use for self-authored plugins


class CalDAVConfig(BaseModel):
    # TODO: Support multiple CalDAV servers — change to dict of named
    # server configs so agents can access calendars across accounts.
    url: str = ""
    username: str = ""
    password: SecretStr | None = None
    default_calendar: str = "personal"


class SecurityConfig(BaseModel):
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
# Root Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file="config.toml",
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
    workspaces: dict[str, WorkspaceConfig] = {}  # [workspaces.<folder_name>]
    commands: CommandWordsConfig = CommandWordsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    cron_jobs: dict[str, CronJobConfig] = {}  # [cron_jobs.<job_name>]
    intervals: IntervalsConfig = IntervalsConfig()
    queue: QueueConfig = QueueConfig()
    channels: ChannelsConfig = ChannelsConfig()
    plugins: dict[str, PluginConfig] = {}
    security: SecurityConfig = SecurityConfig()
    caldav: CalDAVConfig = CalDAVConfig()

    # Sentinels (class-level, not fields)
    OUTPUT_START_MARKER: ClassVar[str] = "---PYNCHY_OUTPUT_START---"
    OUTPUT_END_MARKER: ClassVar[str] = "---PYNCHY_OUTPUT_END---"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Use TOML + env vars only (.env intentionally unsupported)."""
        return (
            init_settings,
            env_settings,
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
        return Path(os.environ.get("HOME", "/Users/user"))

    @cached_property
    def store_dir(self) -> Path:
        return (self.project_root / "store").resolve()

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
        return self.home_dir / ".config" / "pynchy" / "worktrees"

    @cached_property
    def plugins_dir(self) -> Path:
        return self.home_dir / ".config" / "pynchy" / "plugins"


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
