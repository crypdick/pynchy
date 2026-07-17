"""Configuration sub-models — each maps to a ``[section]`` in config.toml.

allow: file-length - task adds a core model; splitting schema is out of scope.

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

import posixpath
from pathlib import Path
from typing import Annotated, Literal, NewType

from pydantic import AfterValidator, BaseModel, Field, SecretStr, field_validator, model_validator

from pynchy.config.caldav import CalDAVConfig
from pynchy.config.refs import parse_chat_ref, parse_connection_ref

# Reference strings whose well-formedness is proven by a validator. Carrying
# the proof in a distinct type (per CONVENTIONS.md "Parse, don't validate") means
# downstream code that reads these fields gets a type that can't be confused with an
# arbitrary str. Both are NewTypes over str, so they remain assignable wherever a
# plain str ref is expected (string interpolation, config round-trip, cascade merge).
ConnectionRefStr = NewType("ConnectionRefStr", str)
ChatRefStr = NewType("ChatRefStr", str)
ProfileName = NewType("ProfileName", str)
ToolName = NewType("ToolName", str)
WorkspaceName = NewType("WorkspaceName", str)
RepoSlug = NewType("RepoSlug", str)
# TODO(config-schema-cutover): propagate these semantic names through host/runtime
# call sites as the remaining config plumbing adopts workspace/tool identity types.

CONNECTION_REF_MESSAGE = "command_center.connection must be connection.<platform>.<name>"
CONNECTION_NAME_MESSAGE = "command_center.connection must be a [connections.<name>] name"
CHAT_REF_MESSAGE = "chat must be connection.<platform>.<name>.chat.<chat>"
CONFIG_NAME_MESSAGE = "config names must not be empty"
REPO_SLUG_MESSAGE = "repo must be an owner/repo slug"
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


def _validated_connection_ref(v: str) -> ConnectionRefStr:
    if parse_connection_ref(v) is None:
        raise ValueError(CONNECTION_REF_MESSAGE)
    return ConnectionRefStr(v)


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


# AfterValidator runs after Pydantic coerces the input to str; for Optional fields it
# is skipped on None. The validator returns the NewType so the field's static type
# carries the parse result — no downstream re-validation.
ValidatedConnectionRef = Annotated[ConnectionRefStr, AfterValidator(_validated_connection_ref)]
ValidatedConnectionName = Annotated[str, AfterValidator(_validated_connection_name)]
ValidatedChatRef = Annotated[ChatRefStr, AfterValidator(_validated_chat_ref)]
ValidatedProfileName = Annotated[ProfileName, AfterValidator(_validated_name)]
ValidatedToolName = Annotated[ToolName, AfterValidator(_validated_name)]
ValidatedRepoSlug = Annotated[RepoSlug, AfterValidator(_validated_repo_slug)]
CodexModelReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]

CONTAINER_REACHABLE_BIND_HOST = "0.0.0.0"  # noqa: S104, RUF100 - agent containers must reach the host gateway through the configured container_host.


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
    timeout_ms: int = 1800000  # 30 minutes — hard per-query wall-clock safety net
    max_output_size: int = 10485760  # 10MB
    # Idle reclamation timer (resets on every output). Kept well under timeout_ms
    # so a finished container hibernates gracefully via the cooperative _close
    # sentinel before the hard timeout would race docker stop -> SIGKILL (137).
    idle_timeout_ms: int = 900000  # 15 minutes
    orphan_reap_age_ms: int = 604800000  # 7 days
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
    host: str = CONTAINER_REACHABLE_BIND_HOST  # bind address
    container_host: str = "host.docker.internal"  # hostname containers use to reach host
    litellm_config: str | None = None  # path to litellm_config.yaml; None = builtin mode
    litellm_image: str = "ghcr.io/berriai/litellm:main-latest"
    postgres_image: str = "postgres:17-alpine"
    master_key: SecretStr | None = None  # LiteLLM master key (required for LiteLLM mode)
    ui_username: str | None = None  # LiteLLM UI login username
    ui_password: SecretStr | None = None  # LiteLLM UI login password


class OneCliConfig(_StrictModel):
    """OneCLI Agent Vault integration — credential isolation for agent containers."""

    enabled: bool = False
    url: str = "http://localhost:10254"
    api_key_env: str = "ONECLI_API_KEY"
    project_id_env: str = "ONECLI_PROJECT_ID"
    fail_closed: bool = True
    agent_identifier_prefix: str = "pynchy"


class ObsidianLearningConfig(_StrictModel):
    vault_root: str | None = None
    mount_path: str = "/workspace/vault"
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
    skill_max_bytes: int = 200_000
    obsidian: ObsidianLearningConfig = ObsidianLearningConfig()

    @field_validator(
        "max_attempts",
        "packet_max_chars",
        "skill_max_bytes",
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
    """Per-channel config for a Discord guild channel.

    Threads inherit their parent channel's config, so this also governs any
    thread opened under the channel.  ``require_mention`` is ``None`` by
    default so an unset channel inherits the guild's value; only an explicit
    bool overrides it.
    """

    name: str | None = None
    enabled: bool = True
    require_mention: bool | None = None
    users: list[str] = []
    roles: list[str] = []
    allow: list[str] = []  # per-channel tool allowlist
    deny: list[str] = []  # per-channel tool denylist (deny wins)
    security: ChannelOverrideConfig | None = None


class DiscordGuildConfig(_StrictModel):
    """Per-guild config for a Discord connection (a ``chat.<guild>`` section)."""

    name: str | None = None
    require_mention: bool = True
    users: list[str] = []  # guild-wide sender allowlist (names or ids)
    roles: list[str] = []  # guild-wide role-id allowlist
    channels: dict[str, DiscordChannelConfig] = {}
    security: ChannelOverrideConfig | None = None


class DiscordConnectionConfig(_StrictModel):
    """Discord connection config (bot token read from an env var).

    ``chat`` is keyed by guild id/slug, mirroring Slack's ``chat`` map, but
    each guild nests a ``channels`` map because one guild channel can host many
    threads.
    """

    type: Literal["discord"] = "discord"
    bot_token_env: str
    application_id: str | None = None
    processing_ack_emoji: str | None = "🦞"
    dm_policy: Literal["open", "allowlist", "disabled"] = "allowlist"
    allow_from: list[str] = []  # DM allowlist (names or ids); "*" = open
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

    connection: ValidatedConnectionName | None = None


def __getattr__(name: str) -> object:
    if name == "CapabilityTomlConfig":
        from pynchy.config.profiles import (  # noqa: PLC0415, RUF100 - lazy re-export keeps imports acyclic.
            CapabilityTomlConfig,
        )

        return CapabilityTomlConfig
    if name == "ProfileConfig":
        from pynchy.config.profiles import (  # noqa: PLC0415, RUF100 - lazy re-export keeps imports acyclic.
            ProfileConfig,
        )

        return ProfileConfig
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)


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
    model: str | None = None


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


class _ToolTrustConfig(_StrictModel):
    enabled: bool = True
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True


class BuiltinTool(_ToolTrustConfig):
    type: Literal["builtin"]
    name: str | None = None


class LinearTool(_ToolTrustConfig):
    type: Literal["linear"]
    workspace: str | None = None
    api_key_env: str | None = None
    project_per_workspace: bool | None = None
    project_name_template: str | None = None


class CalDAVTool(_ToolTrustConfig, CalDAVConfig):
    type: Literal["caldav"]


class McpToolConfig(_StrictModel):
    """MCP provider/runtime config nested under ``[tools.<name>.mcp]``."""

    runtime: Literal["docker", "url", "script"] = "docker"
    image: str | None = None
    dockerfile: str | None = None
    extra_ports: list[int] = []
    command: str | None = None
    args: list[str] = []
    port: int | None = None
    idle_timeout: int = 600
    env: dict[str, str] = {}
    env_forward: dict[str, str] = {}
    onecli: bool = False
    onecli_agent: str = "workspace"
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
        return self


class McpTool(_ToolTrustConfig):
    type: Literal["mcp"]
    mcp: McpToolConfig


ToolConfig = Annotated[
    BuiltinTool | LinearTool | CalDAVTool | McpTool,
    Field(discriminator="type"),
]


ConnectionConfig = Annotated[
    SlackConnectionConfig | WhatsAppConnectionConfig | DiscordConnectionConfig,
    Field(discriminator="type"),
]


class PluginConfig(_StrictModel):
    enabled: bool = True


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
