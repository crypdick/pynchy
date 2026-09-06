"""Configuration sub-models for layered ``pynchy.toml`` settings."""

from __future__ import annotations

# allow: file-length - Pydantic's discriminated settings schemas share validators and aliases here.
# Adapter code uses semantic contracts from their domain APIs, not these Pydantic models.
import posixpath
import re
from pathlib import Path
from typing import Annotated, Literal, NewType

from pydantic import AfterValidator, BaseModel, Field, SecretStr, field_validator, model_validator

from pynchy.config.caldav import CalDAVConfig
from pynchy.config.permissions import PermissionConfig
from pynchy.config.refs import parse_chat_ref
from pynchy.config.workspace_layout import (
    WorkspaceScopeConfig,
    WorkspaceThreadConfig,
)
from pynchy.discord import (
    DiscordAccessSettings,
    DiscordChannelSettings,
    DiscordConnectionSettings,
    DiscordGuildSettings,
)
from pynchy.integration_contracts import (
    MatrixActivation,
    MatrixOutbound,
)

# Reference strings whose well-formedness is proven by a validator. Carrying
# the proof in a distinct type (per CONVENTIONS.md "Parse, don't validate") means
# downstream code that reads these fields gets a type that can't be confused with an
# arbitrary str. Both are NewTypes over str, so they remain assignable wherever a
# plain str ref is expected (string interpolation, config round-trip, cascade merge).
ChatRefStr = NewType("ChatRefStr", str)
ProfileName = NewType("ProfileName", str)
ToolName = NewType("ToolName", str)
WorkspaceName = NewType("WorkspaceName", str)
RepoSlug = NewType("RepoSlug", str)
PromptName = NewType("PromptName", str)
# TODO(config-schema-cutover): propagate these semantic names through host/runtime
# call sites as the remaining config plumbing adopts workspace/tool identity types.

CONNECTION_NAME_MESSAGE = "command_center.connection must be a [connections.<name>] name"
CHAT_REF_MESSAGE = "chat must be connection.<platform>.<name>.chat.<chat>"
CONFIG_NAME_MESSAGE = "config names must not be empty"
REPO_SLUG_MESSAGE = "repo must be an owner/repo slug"
PROMPT_NAME_MESSAGE = (
    "prompt IDs must use souls/, executors/, or reviewers/ with a lowercase "
    "hyphenated filename, or webhooks/ with lowercase hyphenated path components"
)
MOUNT_ABSOLUTE_MESSAGE = "mount_path must be an absolute container path"
MOUNT_POSIX_MESSAGE = "mount_path must be an absolute POSIX container path"
MOUNT_PARENT_MESSAGE = "mount_path must not contain '..' path components"
MOUNT_ROOT_MESSAGE = "mount_path must not mount the vault at '/'"
DEFAULT_PROFILE_RELATIVE_MESSAGE = "default_profile_root must be a relative path template"
DEFAULT_PROFILE_PARENT_MESSAGE = "default_profile_root must not contain '..' path components"
LEARNING_DIR_NAME_MESSAGE = "learning directory names must be a single path component"
LEARNING_POSITIVE_MESSAGE = "learning operational values must be positive"
DOCKER_MCP_IMAGE_MESSAGE = "Docker MCP tools require 'image'"
DOCKER_MCP_PORT_MESSAGE = "Docker MCP tools require 'port'"
URL_MCP_URL_MESSAGE = "URL MCP tools require 'url'"
SCRIPT_MCP_COMMAND_MESSAGE = "Script MCP tools require 'command'"
SCRIPT_MCP_PORT_MESSAGE = "Script MCP tools require 'port'"
STDIO_MCP_COMMAND_MESSAGE = "Stdio MCP tools require 'command'"
STDIO_MCP_PORT_MESSAGE = "Stdio MCP tools require 'port'"
STDIO_MCP_TRANSPORT_MESSAGE = "Stdio MCP tools require HTTP transport"
ENV_NAME_MESSAGE = "tool environment names must use shell variable syntax"


def _validated_connection_name(v: str) -> str:
    if v.startswith("connection.") or "." in v:
        raise ValueError(CONNECTION_NAME_MESSAGE)
    return _validated_name(v)


def _validated_chat_ref(v: str) -> ChatRefStr:
    if parse_chat_ref(v) is None:
        raise ValueError(CHAT_REF_MESSAGE)
    return ChatRefStr(v)


def _validated_name(v: str) -> str:
    if not v.strip():
        raise ValueError(CONFIG_NAME_MESSAGE)
    return v


def _validated_repo_slug(v: str) -> RepoSlug:
    if v.count("/") != 1 or any(not part for part in v.split("/")):
        raise ValueError(REPO_SLUG_MESSAGE)
    return RepoSlug(v)


def _validated_prompt_id(v: str) -> PromptName:
    if (
        re.fullmatch(
            r"(?:souls|executors|reviewers)/[a-z0-9]+(?:-[a-z0-9]+)*"
            r"|webhooks/[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*",
            v,
        )
        is None
    ):
        raise ValueError(PROMPT_NAME_MESSAGE)
    return PromptName(v)


def _validated_env_name(v: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) is None:
        raise ValueError(ENV_NAME_MESSAGE)
    return v


# AfterValidator runs after Pydantic coerces the input to str; for Optional fields it
# is skipped on None. The validator returns the NewType so the field's static type
# carries the parse result — no downstream re-validation.
ValidatedConnectionName = Annotated[str, AfterValidator(_validated_connection_name)]
ValidatedChatRef = Annotated[ChatRefStr, AfterValidator(_validated_chat_ref)]
ValidatedProfileName = Annotated[ProfileName, AfterValidator(_validated_name)]
ValidatedToolName = Annotated[ToolName, AfterValidator(_validated_name)]
ValidatedWorkspaceName = Annotated[WorkspaceName, AfterValidator(_validated_name)]
ValidatedRepoSlug = Annotated[RepoSlug, AfterValidator(_validated_repo_slug)]
ValidatedPromptId = Annotated[PromptName, AfterValidator(_validated_prompt_id)]
ValidatedEnvName = Annotated[str, AfterValidator(_validated_env_name)]
CodexModelReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
CopWireApi = Literal["messages", "responses"]

CONTAINER_REACHABLE_BIND_HOST = "0.0.0.0"  # noqa: S104 - agent containers must reach the host gateway through the configured container_host.
# Keep production on the deterministic runtime's tested LiteLLM digest.
_DEFAULT_LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm@"
    "sha256:9c1f1889774a973ce650f712ace6753a9b6dd1182d25d837b858dbcac6ea3056"
)


def _default_repos_root() -> Path:
    """Use the directory containing the Pynchy checkout for sibling repo clones."""
    return Path.cwd().parent.resolve()


class _StrictModel(BaseModel):
    """Base for all config sub-models — reject unknown keys so typos fail loudly."""

    model_config = {"extra": "forbid"}


class AgentConfig(_StrictModel):
    name: str = "pynchy"
    trigger_aliases: list[str] = ["ghost"]
    default_core: str = "openai"  # built-in: "openai", "claude", "claude-cli", or "codex"
    model: str | None = None
    # NOTE: Update docs/usage/agent-cores.md § OpenAI Codex CLI if this list changes.
    model_reasoning_effort: CodexModelReasoningEffort | None = None


class ContainerConfig(_StrictModel):
    image: str = "pynchy-agent:latest"
    # Maximum silence after an agent starts producing structured output. Initial
    # startup silence has a fixed one-minute bound, and a separate four-times
    # hard ceiling still bounds continuously noisy wedges.
    timeout_ms: int = 1800000  # 30 minutes
    # Agent containers must fit alongside host services on the deployment
    # machine. Full test suites scale their xdist workers to this cgroup cap.
    memory_mb: Annotated[int, Field(gt=0, le=2048)] = 2048
    # Idle reclamation starts only after a query completes.
    idle_timeout_ms: int = 900000  # 15 minutes
    orphan_reap_age_ms: int = 604800000  # 7 days
    max_concurrent: int = 10
    runtime: str | None = None  # "docker" | plugin runtime name (e.g. "apple") | None

    @field_validator("max_concurrent")
    @classmethod
    def clamp_max_concurrent(cls, v: int) -> int:
        return max(1, v)


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


class GatewayConfig(_StrictModel):
    """LLM API gateway — credential isolation for containers.

    Two modes:

    **LiteLLM** (normal service mode): Runs a LiteLLM proxy Docker
    container.  All LLM routing config (models, keys, budgets, load
    balancing) lives in the litellm YAML — no translation needed.

    **Builtin** (component and test mode): Simple aiohttp reverse proxy
    for single-key setups. Normal startup requires personalized LiteLLM config.
    """

    port: int = 4010  # set to 4000 when using litellm mode
    managed: bool = True  # false when an orchestrator owns LiteLLM/PostgreSQL lifecycle
    mcp_proxy_port: Annotated[int, Field(ge=0, le=65535)] = 0  # fixed for cluster Services
    host: str = CONTAINER_REACHABLE_BIND_HOST  # bind address
    container_host: str = "host.docker.internal"  # hostname containers use to reach host
    litellm_config: str | None = None  # convention-wired personalized source path
    litellm_image: str = _DEFAULT_LITELLM_IMAGE
    postgres_image: str = "postgres:17-alpine"
    master_key: SecretStr | None = None  # LiteLLM master key (required for LiteLLM mode)
    ui_username: str | None = None  # LiteLLM UI login username
    ui_password: SecretStr | None = None  # LiteLLM UI login password


class ObsidianLearningConfig(_StrictModel):
    vault_root: str | None = None
    mount_path: str = "/home/agent/memory"
    default_profile_root: str = "systems/pynchy/profiles/{profile}"
    memory_dir_name: str = "memory"

    @field_validator("mount_path")
    @classmethod
    def validate_mount_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(MOUNT_ABSOLUTE_MESSAGE)
        if "\\" in v:
            raise ValueError(MOUNT_POSIX_MESSAGE)
        raw_parts = [part for part in v.split("/") if part]
        if any(part == ".." for part in raw_parts):
            raise ValueError(MOUNT_PARENT_MESSAGE)
        normalized = "/" + "/".join(part for part in raw_parts if part != ".")
        normalized = posixpath.normpath(normalized)
        if normalized == "/":
            raise ValueError(MOUNT_ROOT_MESSAGE)
        return normalized

    @field_validator("default_profile_root")
    @classmethod
    def validate_default_profile_root(cls, v: str) -> str:
        path = Path(v)
        if path.is_absolute():
            raise ValueError(DEFAULT_PROFILE_RELATIVE_MESSAGE)
        if any(part == ".." for part in path.parts):
            raise ValueError(DEFAULT_PROFILE_PARENT_MESSAGE)
        return v

    @field_validator("memory_dir_name")
    @classmethod
    def validate_learning_dir_name(cls, v: str) -> str:
        if not v or "/" in v or v in {".", ".."}:
            raise ValueError(LEARNING_DIR_NAME_MESSAGE)
        return v


class LearningConfig(_StrictModel):
    enabled: bool = False
    review_after_turn: bool = True
    max_attempts: int = 3
    packet_max_chars: int = 12_000
    obsidian: ObsidianLearningConfig = ObsidianLearningConfig()

    @field_validator(
        "max_attempts",
        "packet_max_chars",
    )
    @classmethod
    def validate_positive_operational_knobs(cls, v: float | int) -> float | int:
        if v <= 0:
            raise ValueError(LEARNING_POSITIVE_MESSAGE)
        return v


class OwnerConfig(_StrictModel):
    """Owner identity per platform — used for allowed_users = ["owner"] resolution."""

    # Prefer human display/user names; Slack user IDs are accepted.
    slack: str | None = None
    # WhatsApp uses is_from_me, no config needed


class ChannelOverrideConfig(_StrictModel):
    """Per-connection or per-chat sender allowlist."""

    allowed_users: list[str] | None = None


class ConnectionChatConfig(_StrictModel):
    """Per-chat security overrides for a connection."""

    security: ChannelOverrideConfig | None = None


class SlackConnectionConfig(_StrictModel):
    """Slack connection config (tokens are read from env vars)."""

    type: Literal["slack"] = "slack"
    bot_token_env: str
    app_token_env: str
    security: ChannelOverrideConfig | None = None
    chat: dict[str, ConnectionChatConfig] = {}


class WhatsAppConnectionConfig(_StrictModel):
    """WhatsApp connection config (auth state stored in sqlite)."""

    type: Literal["whatsapp"] = "whatsapp"
    auth_db_path: str | None = None
    security: ChannelOverrideConfig | None = None
    chat: dict[str, ConnectionChatConfig] = {}


class DiscordChannelConfig(_StrictModel):
    """Per-channel configuration for a Discord guild channel."""

    name: str | None = None
    kind: Literal["text", "voice", "forum"] = "text"
    category: str | None = None
    enabled: bool = True
    require_mention: bool | None = None
    users: list[str] = []
    roles: list[str] = []
    allow: list[str] = []
    deny: list[str] = []
    secret_collections: list[str] = []
    security: ChannelOverrideConfig | None = None


class DiscordGuildConfig(_StrictModel):
    """Per-guild configuration for a Discord connection."""

    name: str | None = None
    require_mention: bool = True
    users: list[str] = []
    roles: list[str] = []
    channels: dict[str, DiscordChannelConfig] = {}
    security: ChannelOverrideConfig | None = None


class DiscordConnectionConfig(_StrictModel):
    """Discord connection configuration (the bot token is an environment variable)."""

    type: Literal["discord"] = "discord"
    bot_token_env: str
    application_id: str | None = None
    processing_ack_emoji: str | None = "🦞"
    default_thread_participants: list[str] = []
    dm_policy: Literal["open", "allowlist", "disabled"] = "allowlist"
    allow_from: list[str] = []
    group_policy: Literal["open", "disabled", "allowlist"] = "allowlist"
    security: ChannelOverrideConfig | None = None
    chat: dict[str, DiscordGuildConfig] = {}

    @model_validator(mode="after")
    def validate_single_voice_channel(self) -> DiscordConnectionConfig:
        """Keep one Discord connection bound to one live voice session."""
        voice_channels = [
            f"{guild_name}.channels.{channel_name}"
            for guild_name, guild in self.chat.items()
            for channel_name, channel in guild.channels.items()
            if channel.kind == "voice"
        ]
        if len(voice_channels) > 1:
            joined = ", ".join(voice_channels)
            raise ValueError(
                "Discord supports one active Pynchy voice channel per connection; "
                f"configure one voice channel, not: {joined}"
            )
        return self

    def to_runtime_settings(self) -> DiscordConnectionSettings:
        """Resolve validated TOML data into the Discord channel's domain values."""
        return DiscordConnectionSettings(
            bot_token_env=self.bot_token_env,
            application_id=self.application_id,
            processing_ack_emoji=self.processing_ack_emoji,
            default_thread_participants=list(self.default_thread_participants),
            dm_policy=self.dm_policy,
            allow_from=list(self.allow_from),
            group_policy=self.group_policy,
            security=_discord_access_settings(self.security),
            chat={
                guild_name: DiscordGuildSettings(
                    name=guild.name,
                    require_mention=guild.require_mention,
                    users=list(guild.users),
                    roles=list(guild.roles),
                    security=_discord_access_settings(guild.security),
                    channels={
                        channel_name: DiscordChannelSettings(
                            name=channel.name,
                            kind=channel.kind,
                            category=channel.category,
                            enabled=channel.enabled,
                            require_mention=channel.require_mention,
                            users=list(channel.users),
                            roles=list(channel.roles),
                            allow=list(channel.allow),
                            deny=list(channel.deny),
                            secret_collections=list(channel.secret_collections),
                            security=_discord_access_settings(channel.security),
                        )
                        for channel_name, channel in guild.channels.items()
                    },
                )
                for guild_name, guild in self.chat.items()
            },
        )


def _discord_access_settings(
    config: ChannelOverrideConfig | None,
) -> DiscordAccessSettings | None:
    if config is None:
        return None
    allowed_users = config.allowed_users
    return DiscordAccessSettings(list(allowed_users) if allowed_users is not None else None)


class CommandCenterConfig(_StrictModel):
    """Which connection is the dedicated command center."""

    connection: ValidatedConnectionName | None = None


class NotificationsConfig(_StrictModel):
    """Route host lifecycle notifications to one designated admin workspace."""

    admin_workspace: ValidatedWorkspaceName | None = None


class OpsConfig(_StrictModel):
    """Private target for fixed remote operator diagnostics."""

    ssh_host: str | None = None
    namespace: str | None = None

    @field_validator("ssh_host")
    @classmethod
    def validate_ssh_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
            raise ValueError("ops.ssh_host must be a safe SSH host alias")
        return value

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value) is None:
            raise ValueError("ops.namespace must be a Kubernetes DNS label")
        return value


class RepoConfig(_StrictModel):
    """Config for a single tracked git repo under [repos."owner/repo"]."""

    path: str | None = None  # relative to project root or absolute; None = repos.root / repo_name
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
    profiles: list[ValidatedProfileName] = Field(default_factory=list)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    soul: ValidatedPromptId | None = None
    pipeline: str | None = None
    model: str | None = None
    model_reasoning_effort: CodexModelReasoningEffort | None = None
    chat: ValidatedChatRef | None = None

    threads: list[WorkspaceThreadConfig] = Field(default_factory=list)
    scopes: list[WorkspaceScopeConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_thread_names_are_unique(self) -> WorkspaceConfig:
        names = [thread.name.casefold() for thread in self.threads]
        if len(names) != len(set(names)):
            raise ValueError("workspace thread names must be unique ignoring case")
        return self

    @field_validator("pipeline")
    @classmethod
    def validate_pipeline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        pipeline = value.strip()
        if not pipeline:
            raise ValueError("workspace pipeline cannot be empty")
        return pipeline

    @field_validator("soul")
    @classmethod
    def validate_soul(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("souls/"):
            raise ValueError("workspace soul must use the souls/ scope")
        return value


class RouteConfig(_StrictModel):
    """Provider-neutral placement and restriction for one exact endpoint."""

    source: ValidatedChatRef
    workspace: ValidatedWorkspaceName
    activation: Literal["on_event", "on_demand"] | None = None
    outbound: Literal["read_only", "approval_required"] | None = None
    tools: tuple[ValidatedToolName, ...] | None = None
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)


class ReposConfig(_StrictModel):
    root: Path = Field(default_factory=_default_repos_root)
    overrides: dict[str, RepoConfig] = Field(default_factory=dict)

    @field_validator("root", mode="before")
    @classmethod
    def resolve_root(cls, v: str | Path) -> Path:
        p = Path(v).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve()

    @model_validator(mode="before")
    @classmethod
    def reject_inline_repo_overrides(cls, data: dict[str, object]) -> dict[str, object]:
        if not isinstance(data, dict):
            return data
        unknown = sorted(set(data) - {"root", "overrides"})
        if unknown:
            message = f"repo overrides must be nested under repos.overrides: {unknown}"
            raise ValueError(message)
        return data

    @field_validator("overrides")
    @classmethod
    def validate_override_slugs(cls, v: dict[str, RepoConfig]) -> dict[str, RepoConfig]:
        for slug in v:
            _validated_repo_slug(slug)
        return v


class _ToolAccessConfig(_StrictModel):
    enabled: bool = True
    skills: list[str] = Field(default_factory=list)
    required_env: list[ValidatedEnvName] = Field(default_factory=list)
    optional_env: list[ValidatedEnvName] = Field(default_factory=list)
    expose_env_to_workspace: bool = False
    # NOTE: Update docs/usage/security.md § Permissions if tool permission defaults change.
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)

    @model_validator(mode="after")
    def validate_environment_names(self) -> _ToolAccessConfig:
        if len(self.required_env) != len(set(self.required_env)):
            raise ValueError("tool required_env names must be unique")
        if len(self.optional_env) != len(set(self.optional_env)):
            raise ValueError("tool optional_env names must be unique")
        overlap = sorted(set(self.required_env) & set(self.optional_env))
        if overlap:
            raise ValueError(
                "tool environment names cannot be both required and optional: " + ", ".join(overlap)
            )
        return self


class _ToolTrustConfig(_ToolAccessConfig):
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True


class BuiltinTool(_ToolTrustConfig):
    type: Literal["builtin"]
    name: str | None = None


class CalDAVTool(_ToolTrustConfig, CalDAVConfig):
    type: Literal["caldav"]


class LinearTool(_ToolTrustConfig):
    """One Linear credential plus its security-policy declarations."""

    type: Literal["linear"]
    workspace: str | None = None
    required_env: list[ValidatedEnvName] = Field(default_factory=lambda: ["LINEAR_API_KEY"])
    optional_env: list[ValidatedEnvName] = Field(default_factory=lambda: ["LINEAR_TEAM_KEY"])
    project_per_workspace: bool | None = None  # noqa: V107
    project_name_template: str | None = None  # noqa: V107

    @model_validator(mode="after")
    def validate_linear_environment_shape(self) -> LinearTool:
        if len(self.required_env) != 1:
            raise ValueError("Linear tools require exactly one required_env API key")
        if len(self.optional_env) > 1:
            raise ValueError("Linear tools accept at most one optional_env team key")
        return self

    @property
    def api_key_env(self) -> str:
        return self.required_env[0]

    @property
    def team_key_env(self) -> str:
        return self.optional_env[0] if self.optional_env else "LINEAR_TEAM_KEY"


class WorkspaceTool(_ToolAccessConfig):
    """Agent-side capability with no host service or MCP runtime."""

    type: Literal["workspace"]
    expose_env_to_workspace: Literal[True] = True


class McpToolConfig(_StrictModel):
    """MCP provider/runtime config nested under ``[tools.<name>.mcp]``."""

    runtime: Literal["docker", "url", "script", "stdio"] = "docker"
    image: str | None = None
    dockerfile: str | None = None
    build_context: str = "."
    extra_ports: list[int] = []
    command: str | None = None
    args: list[str] = []
    port: int | None = None
    idle_timeout: int = 600
    startup_timeout_seconds: Annotated[float, Field(gt=0)] = 5.0
    env: dict[str, str] = {}
    volumes: list[str] = []
    inject_workspace: bool = False
    url: str | None = None
    transport: Literal["sse", "http", "streamable_http"] = "sse"
    auth_value_env: str | None = None
    credentials_path: str | None = None

    @model_validator(mode="after")
    def validate_explicit_runtime_config(self) -> McpToolConfig:
        if self.runtime == "docker":
            if not self.image:
                raise ValueError(DOCKER_MCP_IMAGE_MESSAGE)
            if self.port is None:
                raise ValueError(DOCKER_MCP_PORT_MESSAGE)
        elif self.runtime == "url":
            if not self.url:
                raise ValueError(URL_MCP_URL_MESSAGE)
        elif self.runtime == "script":
            if not self.command:
                raise ValueError(SCRIPT_MCP_COMMAND_MESSAGE)
            if self.port is None:
                raise ValueError(SCRIPT_MCP_PORT_MESSAGE)
        else:
            if not self.command:
                raise ValueError(STDIO_MCP_COMMAND_MESSAGE)
            if self.port is None:
                raise ValueError(STDIO_MCP_PORT_MESSAGE)
            if self.transport not in ("http", "streamable_http"):
                raise ValueError(STDIO_MCP_TRANSPORT_MESSAGE)
        return self


class McpTool(_ToolTrustConfig):
    type: Literal["mcp"]
    mcp: McpToolConfig


_MATRIX_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_MATRIX_ROOM_ID = re.compile(r"!\S+:\S+")
_MATRIX_USER_ID = re.compile(r"@\S+:\S+")


class MatrixRouteDefaults(_StrictModel):
    """Connection-level defaults inherited by exact routes."""

    model_config = {"frozen": True}
    activation: MatrixActivation = "on_demand"
    outbound: MatrixOutbound = "approval_required"


class MatrixEndpointConfig(_StrictModel):
    """One immutable Matrix room endpoint with optional bridge assertions."""

    model_config = {"frozen": True}
    room_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    expected_bridge: str | None = Field(default=None, min_length=1)
    require_active_portal: bool = False
    enabled: bool = True

    @field_validator("room_id")
    @classmethod
    def validate_room_id(cls, value: str) -> str:
        if not _MATRIX_ROOM_ID.fullmatch(value):
            raise ValueError("Matrix endpoint room_id must be an immutable Matrix room ID")
        return value


class MatrixConnectionConfig(_StrictModel):
    """One authenticated Matrix owner identity and its named endpoints."""

    model_config = {"frozen": True}
    type: Literal["matrix"] = "matrix"
    gateway_command_env: str = "PYNCHY_MATRIX_GATEWAY"
    expected_user_id: str = Field(min_length=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    route_defaults: MatrixRouteDefaults = Field(default_factory=MatrixRouteDefaults)
    chat: dict[str, MatrixEndpointConfig] = Field(default_factory=dict)

    @field_validator("gateway_command_env")
    @classmethod
    def validate_gateway_command_env(cls, value: str) -> str:
        if not _MATRIX_ENV_NAME.fullmatch(value):
            raise ValueError("gateway_command_env must name an environment variable")
        return value

    @field_validator("expected_user_id")
    @classmethod
    def validate_expected_user_id(cls, value: str) -> str:
        if not _MATRIX_USER_ID.fullmatch(value):
            raise ValueError("expected_user_id must be a full Matrix user ID")
        return value


ToolConfig = Annotated[
    BuiltinTool | LinearTool | CalDAVTool | McpTool | WorkspaceTool,
    Field(discriminator="type"),
]


ChannelConnectionConfig = SlackConnectionConfig | WhatsAppConnectionConfig | DiscordConnectionConfig
ConnectionConfig = Annotated[
    ChannelConnectionConfig | MatrixConnectionConfig, Field(discriminator="type")
]


class PluginConfig(_StrictModel):
    enabled: bool = True
    # Plugin-specific models parse this untyped transport at plugin registration.
    options: dict[str, object] = Field(default_factory=dict)


class SecurityConfig(_StrictModel):
    # NOTE: Update docs/usage/security.md § Agent Tool Gating if you change
    # the Cop model selection contract.
    cop_model: str | None = None
    cop_wire_api: CopWireApi = "messages"
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
