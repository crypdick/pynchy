"""Configuration sub-models — each maps to a ``[section]`` in config.toml.

Extracted from :mod:`pynchy.config` to keep the root Settings class
focused on composition and validation.  Follows the same pattern as
:mod:`pynchy.config_mcp`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from croniter import croniter
from pydantic import BaseModel, SecretStr, field_validator

from pynchy.config_refs import parse_chat_ref, parse_connection_ref


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


class ConnectionChatConfig(_StrictModel):
    """Per-chat security overrides for a connection."""

    security: "ChannelOverrideConfig" | None = None


class SlackConnectionConfig(_StrictModel):
    """Slack connection config (tokens are read from env vars)."""

    bot_token_env: str
    app_token_env: str
    security: "ChannelOverrideConfig" | None = None
    chat: dict[str, ConnectionChatConfig] = {}

    @field_validator("bot_token_env", "app_token_env")
    @classmethod
    def _validate_env_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("env var name cannot be empty")
        return v


class WhatsAppConnectionConfig(_StrictModel):
    """WhatsApp connection config (auth state stored in sqlite)."""

    auth_db_path: str | None = None
    security: "ChannelOverrideConfig" | None = None
    chat: dict[str, ConnectionChatConfig] = {}


class ConnectionsConfig(_StrictModel):
    """Root container for all external chat connections."""

    slack: dict[str, SlackConnectionConfig] = {}
    whatsapp: dict[str, WhatsAppConnectionConfig] = {}


class CommandCenterConfig(_StrictModel):
    """Which connection is the dedicated command center."""

    connection: str | None = None

    @field_validator("connection")
    @classmethod
    def _validate_connection_ref(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if parse_connection_ref(v) is None:
            raise ValueError("command_center.connection must be connection.<platform>.<name>")
        return v


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
    # FIXME: Rename "workspace" -> "sandbox" across config + codebase.
    name: str | None = None  # display name — optional, derived when omitted
    # TODO: Allow binding to a whole connection (not just a chat).
    chat: str | None = None  # connection.<platform>.<name>.chat.<chat>
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
    git_policy: Literal["merge-to-main", "pull-request"] | None = None  # None → merge-to-main
    idle_terminate: bool = True  # False → container stays alive until hard timeout

    @field_validator("chat")
    @classmethod
    def validate_chat_ref(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if parse_chat_ref(v) is None:
            raise ValueError("chat must be connection.<platform>.<name>.chat.<chat>")
        return v

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


class PluginConfig(_StrictModel):
    enabled: bool = True


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
