"""Configuration sub-models — each maps to a ``[section]`` in config.toml.

Extracted from :mod:`pynchy.config` to keep the root Settings class
focused on composition and validation.  Follows the same pattern as
:mod:`pynchy.config.mcp`.

ARCHITECTURE NOTE: Plugin-specific config models belong in the plugin's own
source file, not here. This file should only contain models for pynchy core
settings (agent, container, gateway, connections, etc.). Built-in plugins
(CalDAV, Slack) keep their models here as an exception; they ideally belong
in their respective plugin files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, NewType

from croniter import croniter
from pydantic import AfterValidator, BaseModel, SecretStr, field_validator

from pynchy.config.refs import parse_chat_ref, parse_connection_ref

# Reference strings whose well-formedness is proven by a validator. Carrying
# the proof in a distinct type (per CONVENTIONS.md "Parse, don't validate") means
# downstream code that reads these fields gets a type that can't be confused with an
# arbitrary str. Both are NewTypes over str, so they remain assignable wherever a
# plain str ref is expected (string interpolation, config round-trip, cascade merge).
ConnectionRefStr = NewType("ConnectionRefStr", str)
ChatRefStr = NewType("ChatRefStr", str)


def _validated_connection_ref(v: str) -> ConnectionRefStr:
    if parse_connection_ref(v) is None:
        raise ValueError("command_center.connection must be connection.<platform>.<name>")
    return ConnectionRefStr(v)


def _validated_chat_ref(v: str) -> ChatRefStr:
    if parse_chat_ref(v) is None:
        raise ValueError("chat must be connection.<platform>.<name>.chat.<chat>")
    return ChatRefStr(v)


# AfterValidator runs after Pydantic coerces the input to str; for Optional fields it
# is skipped on None. The validator returns the NewType so the field's static type
# carries the parse result — no downstream re-validation.
ValidatedConnectionRef = Annotated[ConnectionRefStr, AfterValidator(_validated_connection_ref)]
ValidatedChatRef = Annotated[ChatRefStr, AfterValidator(_validated_chat_ref)]


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
    timeout_ms: int = 1800000  # 30 minutes — hard per-query wall-clock safety net
    max_output_size: int = 10485760  # 10MB
    # Idle reclamation timer (resets on every output). Kept well under timeout_ms
    # so a finished container hibernates gracefully via the cooperative _close
    # sentinel before the hard timeout would race docker stop -> SIGKILL (137).
    idle_timeout_ms: int = 900000  # 15 minutes
    max_concurrent: int = 10
    runtime: str | None = None  # "docker" | plugin runtime name (e.g. "apple") | None

    @field_validator("max_concurrent")
    @classmethod
    def clamp_max_concurrent(cls, v: int) -> int:
        return max(1, v)


class ServerConfig(_StrictModel):
    # NOTE: Update docs/install.md (§ Headless Server Deployment — the 8484
    # references and "Port 8484 not reachable" troubleshooting) if you change this default.
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


class ConnectionChatConfig(_StrictModel):
    """Per-chat security overrides for a connection."""

    security: ChannelOverrideConfig | None = None


class SlackConnectionConfig(_StrictModel):
    """Slack connection config (tokens are read from env vars)."""

    bot_token_env: str
    app_token_env: str
    security: ChannelOverrideConfig | None = None
    chat: dict[str, ConnectionChatConfig] = {}


class WhatsAppConnectionConfig(_StrictModel):
    """WhatsApp connection config (auth state stored in sqlite)."""

    auth_db_path: str | None = None
    security: ChannelOverrideConfig | None = None
    chat: dict[str, ConnectionChatConfig] = {}


class DiscordChannelConfig(_StrictModel):
    """Per-channel config for a Discord guild channel.

    Threads inherit their parent channel's config, so this also governs any
    thread opened under the channel.  ``require_mention`` is ``None`` by
    default so an unset channel inherits the guild's value; only an explicit
    bool overrides it.
    """

    enabled: bool = True
    require_mention: bool | None = None
    users: list[str] = []
    roles: list[str] = []
    allow: list[str] = []  # per-channel tool allowlist
    deny: list[str] = []  # per-channel tool denylist (deny wins)
    security: ChannelOverrideConfig | None = None


class DiscordGuildConfig(_StrictModel):
    """Per-guild config for a Discord connection (a ``chat.<guild>`` section)."""

    require_mention: bool = True
    users: list[str] = []  # guild-wide sender allowlist (ids)
    roles: list[str] = []  # guild-wide role-id allowlist
    channels: dict[str, DiscordChannelConfig] = {}
    security: ChannelOverrideConfig | None = None


class DiscordConnectionConfig(_StrictModel):
    """Discord connection config (bot token read from an env var).

    ``chat`` is keyed by guild id/slug, mirroring Slack's ``chat`` map, but
    each guild nests a ``channels`` map because one guild channel can host many
    threads.
    """

    bot_token_env: str
    application_id: str | None = None
    dm_policy: Literal["open", "allowlist", "disabled"] = "allowlist"
    allow_from: list[str] = []  # DM allowlist (user snowflakes); "*" = open
    group_policy: Literal["open", "disabled", "allowlist"] = "allowlist"
    security: ChannelOverrideConfig | None = None
    chat: dict[str, DiscordGuildConfig] = {}


class ConnectionsConfig(_StrictModel):
    """Root container for all external chat connections.

    Each field is a ``dict[str, <PlatformConfig>]`` keyed by connection name.
    Use :meth:`get_connection` for platform-generic lookups so callers don't
    need to hardcode platform names.
    """

    slack: dict[str, SlackConnectionConfig] = {}
    whatsapp: dict[str, WhatsAppConnectionConfig] = {}
    discord: dict[str, DiscordConnectionConfig] = {}

    def get_connection(
        self, platform: str, name: str
    ) -> SlackConnectionConfig | WhatsAppConnectionConfig | DiscordConnectionConfig | None:
        """Look up a connection config by platform and name.

        Uses ``getattr`` so any platform field works automatically without
        callers needing ``if/elif`` chains for each platform.
        """
        platform_dict = getattr(self, platform, None)
        if isinstance(platform_dict, dict):
            return platform_dict.get(name)
        return None


class CommandCenterConfig(_StrictModel):
    """Which connection is the dedicated command center."""

    connection: ValidatedConnectionRef | None = None


class SandboxProfileConfig(_StrictModel):
    """Overridable sandbox config — used for sandbox_universal and sandbox_profiles.

    All fields default to None ("no opinion at this tier, inherit from next").
    Use model_fields_set to distinguish "explicitly set" from "defaulted to None".

    List fields (directives, skills, mcp_servers): unioned across tiers.
    Override fields (all others): most-specific explicitly-set value wins.
    """

    # Union fields (merged across tiers, deduplicated)
    directives: list[str] | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None

    # Override fields (most-specific wins)
    context_mode: Literal["group", "isolated"] | None = None
    access: Literal["read", "write", "readwrite"] | None = None
    mode: Literal["agent", "chat"] | None = None
    trust: bool | None = None
    trigger: Literal["mention", "always"] | None = None
    allowed_users: list[str] | None = None  # override semantics, not union
    idle_terminate: bool | None = None
    git_policy: Literal["merge-to-main", "pull-request"] | None = None
    security: WorkspaceSecurityTomlConfig | None = None
    repo_access: str | None = None


class ServiceTrustTomlConfig(_StrictModel):
    """Per-service trust config in config.toml [services.<name>]."""

    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True


class WorkspaceServiceOverride(_StrictModel):
    """Per-workspace service override — only 'forbidden' is allowed.

    All fields are optional (None = no override). Any non-None value
    must be 'forbidden'. This prevents accidentally relaxing security.
    """

    public_source: Literal["forbidden"] | None = None
    secret_data: None = None  # secret_data cannot be overridden
    public_sink: Literal["forbidden"] | None = None
    dangerous_writes: Literal["forbidden"] | None = None


class WorkspaceSecurityTomlConfig(_StrictModel):
    """Security profile in config.toml [workspaces.<name>.security]."""

    services: dict[str, ServiceTrustTomlConfig] = {}
    contains_secrets: bool = False


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
    profile: str | None = None  # sandbox_profiles.<name> reference
    directives: list[str] | None = None  # directive names; convention: directives/<name>.md
    # TODO: Allow binding to a whole connection (not just a chat).
    chat: ValidatedChatRef | None = None  # connection.<platform>.<name>.chat.<chat>
    is_admin: bool | None = None  # None → not admin
    repo_access: str | None = None  # GitHub slug (owner/repo) from [repos.*]; None = no worktree
    schedule: str | None = None  # cron expression
    prompt: str | None = None  # prompt for scheduled tasks
    context_mode: str | None = None  # None → use sandbox_universal
    security: WorkspaceSecurityTomlConfig | None = None  # Trust-based security profile
    skills: list[str] | None = None  # tier names and/or skill names; None = core only
    mcp_servers: list[str] | None = None  # server names + group names, set-unioned
    mcp: dict[str, dict[str, Any]] = {}  # {server_name: {key: value}} → per-MCP kwargs
    # Channel access modes (None → inherit from sandbox_universal/profile)
    access: Literal["read", "write", "readwrite"] | None = None
    mode: Literal["agent", "chat"] | None = None
    trust: bool | None = None
    trigger: Literal["mention", "always"] | None = None
    allowed_users: list[str] | None = None
    git_policy: Literal["merge-to-main", "pull-request"] | None = None  # None → merge-to-main
    idle_terminate: bool | None = None  # None → inherit from profile/universal (default: True)

    # A cron expression has no distinct parsed type worth carrying: croniter.is_valid
    # is cheap and compute_next_run re-instantiates croniter from the string anyway, so
    # this stays a str-returning check rather than a NewType parse.
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

    # See WorkspaceConfig.validate_cron: cron stays a str-returning check because
    # croniter re-validation is cheap and there is no parsed type to carry downstream.
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
    password_env: str | None = None  # env var name; resolves at runtime via os.environ
    default_calendar: str | None = None  # what "primary" resolves to; None → first discovered
    allow: list[str] | None = None  # only expose these calendars (case-insensitive)
    ignore: list[str] | None = None  # hide these (case-insensitive; ignored if allow set)


class CalDAVConfig(_StrictModel):
    default_server: str = ""  # which server to use when no server prefix given
    servers: dict[str, CalDAVServerConfig] = {}


class SecurityConfig(_StrictModel):
    # NOTE: Update docs/architecture/security.md § 2 (Mount Security → Default
    # Blocked Patterns) if you change this list — it restates these values in prose.
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
