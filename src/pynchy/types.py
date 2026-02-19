"""Data models for Pynchy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass
class AdditionalMount:
    host_path: str  # Absolute path on host (supports ~ for home)
    container_path: str | None = None  # Defaults to basename of host_path
    readonly: bool = True  # Default: true for safety


@dataclass
class AllowedRoot:
    path: str  # Absolute path or ~ for home
    allow_read_write: bool = False
    description: str | None = None


@dataclass
class MountAllowlist:
    allowed_roots: list[AllowedRoot] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    non_admin_read_only: bool = True


@dataclass
class ContainerConfig:
    additional_mounts: list[AdditionalMount] = field(default_factory=list)
    timeout: float | None = None  # Seconds (default: 300)

    @classmethod
    def from_dict(cls, raw: dict) -> ContainerConfig:
        return cls(
            additional_mounts=[AdditionalMount(**m) for m in raw.get("additional_mounts", [])],
            timeout=raw.get("timeout"),
        )


@dataclass
class McpToolConfig:
    """Configuration for a single MCP tool."""

    risk_tier: Literal["always-approve", "rules-engine", "human-approval"]
    enabled: bool = True


@dataclass
class RateLimitConfig:
    """Rate limiting configuration for a workspace."""

    max_calls_per_hour: int = 500  # Global limit across all tools
    per_tool_overrides: dict[str, int] = field(default_factory=dict)  # tool -> max/hour


@dataclass
class WorkspaceSecurity:
    """Security configuration for a workspace."""

    # MCP tool permissions (tool_name -> config)
    mcp_tools: dict[str, McpToolConfig] = field(default_factory=dict)

    # Default risk tier for tools not explicitly listed in mcp_tools
    default_risk_tier: Literal["always-approve", "rules-engine", "human-approval"] = (
        "human-approval"
    )

    # Rate limiting (None = no limits)
    rate_limits: RateLimitConfig | None = None

    # Filesystem and network access
    allow_filesystem_access: bool = True
    allow_network_access: bool = True


@dataclass
class WorkspaceProfile:
    """Complete workspace configuration with security profile."""

    # Identity
    jid: str  # Canonical chat identifier
    name: str  # Display name
    folder: str  # Folder under groups/

    # Communication
    trigger: str  # @mention to activate (e.g., "@Pynchy")
    requires_trigger: bool = True  # Whether trigger is required (False for 1-on-1 chats)

    # Container runtime
    container_config: ContainerConfig | None = None

    # Security profile (Phase B.1)
    security: WorkspaceSecurity = field(default_factory=WorkspaceSecurity)

    # Privileges
    is_admin: bool = False

    # Metadata
    added_at: str = ""

    def validate(self) -> list[str]:
        """Validate workspace configuration.

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        # Validate required fields
        if not self.name:
            errors.append("Workspace name is required")
        if not self.folder:
            errors.append("Workspace folder is required")
        if not self.trigger:
            errors.append("Workspace trigger is required")

        # Validate MCP tool risk tiers
        valid_tiers = {"always-approve", "rules-engine", "human-approval"}
        for tool_name, config in self.security.mcp_tools.items():
            if config.risk_tier not in valid_tiers:
                errors.append(
                    f"Invalid risk tier '{config.risk_tier}' for tool '{tool_name}'. "
                    f"Must be one of: {', '.join(valid_tiers)}"
                )

        # Validate default risk tier
        if self.security.default_risk_tier not in valid_tiers:
            errors.append(
                f"Invalid default risk tier '{self.security.default_risk_tier}'. "
                f"Must be one of: {', '.join(valid_tiers)}"
            )

        # Validate rate limits
        rl = self.security.rate_limits
        if rl is not None:
            if not isinstance(rl.max_calls_per_hour, int) or rl.max_calls_per_hour < 1:
                errors.append(
                    f"Invalid max_calls_per_hour: {rl.max_calls_per_hour} "
                    "(must be a positive integer)"
                )
            for tool_name, limit in rl.per_tool_overrides.items():
                if not isinstance(limit, int) or limit < 1:
                    errors.append(
                        f"Invalid per-tool rate limit for '{tool_name}': {limit} "
                        "(must be a positive integer)"
                    )

        return errors


@dataclass
class ResolvedChannelConfig:
    """Fully-resolved channel access configuration — no None fields.

    Produced by resolve_channel_config() after walking the cascade:
    workspace_defaults → workspace → per-channel override.
    """

    access: Literal["read", "write", "readwrite"]
    mode: Literal["agent", "chat"]
    trust: bool
    trigger: Literal["mention", "always"]
    allowed_users: list[str]


@dataclass
class NewMessage:
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool | None = None
    message_type: str = "user"  # 'user', 'assistant', 'system', 'host', 'tool_result'
    metadata: dict | None = None


@dataclass
class ScheduledTask:
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["group", "isolated"]
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str = ""
    repo_access: str | None = None  # GitHub slug (owner/repo); None = no worktree

    def to_snapshot_dict(self) -> dict[str, str | None]:
        """Serialize to the dict format expected by write_tasks_snapshot.

        Used by both app.py and task_scheduler.py to avoid duplicating the
        field mapping when building the tasks snapshot for containers.
        """
        return {
            "id": self.id,
            "type": "agent",
            "groupFolder": self.group_folder,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "status": self.status,
            "next_run": self.next_run,
        }


@dataclass
class TaskRunLog:
    task_id: str
    run_at: str
    duration_ms: float
    status: Literal["success", "error"]
    result: str | None = None
    error: str | None = None


@dataclass
class HostJob:
    """Host-level cron job that runs shell commands directly (no LLM/container).

    NOTE: Future improvement - these should be reviewed by deputy agent
    before being persisted to ensure safety and prevent privilege escalation.
    """

    id: str
    name: str
    command: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    created_by: str
    next_run: str | None = None
    last_run: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str = ""
    cwd: str | None = None
    timeout_seconds: int = 600
    enabled: bool = True

    def to_snapshot_dict(self) -> dict[str, str | None]:
        """Serialize to the dict format expected by write_tasks_snapshot."""
        return {
            "id": self.id,
            "type": "host",
            "name": self.name,
            "command": self.command,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "status": self.status,
            "next_run": self.next_run,
        }


@dataclass
class ContainerInput:
    messages: list[dict]  # SDK message list with message types
    group_folder: str
    chat_jid: str
    is_admin: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    system_notices: list[str] | None = None
    repo_access: str | None = None  # GitHub slug (owner/repo); None = no worktree
    agent_core_module: str = "agent_runner.cores.claude"  # Module path for agent core
    agent_core_class: str = "ClaudeAgentCore"  # Class name for agent core
    agent_core_config: dict | None = None  # Core-specific settings
    mcp_gateway_url: str | None = None  # LiteLLM MCP gateway URL (SSE transport)
    mcp_gateway_key: str | None = None  # LiteLLM virtual key for workspace's MCP team


@dataclass
class ContainerOutput:
    status: Literal["success", "error"]
    result: str | None = None
    new_session_id: str | None = None
    error: str | None = None
    type: str = "result"
    thinking: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    text: str | None = None
    # Transparent token stream fields
    system_subtype: str | None = None
    system_data: dict | None = None
    tool_result_id: str | None = None
    tool_result_content: str | None = None
    tool_result_is_error: bool | None = None
    result_metadata: dict | None = None


@dataclass
class PluginVerification:
    """Cached plugin security verification result."""

    plugin_name: str
    git_sha: str
    verified_at: str
    verdict: Literal["pass", "fail"]
    reasoning: str
    model: str


@dataclass
class VolumeMount:
    host_path: str
    container_path: str
    readonly: bool = False


# --- Channel abstraction ---


@runtime_checkable
class Channel(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def send_message(self, jid: str, text: str) -> None: ...

    def is_connected(self) -> bool:
        """Return True iff the channel can currently receive inbound events.

        Must reflect *actual* liveness — not a stale flag from connect().
        Implementations backed by an asyncio task must check whether that
        task is still running, not just whether connect() was called.
        """
        ...

    def owns_jid(self, jid: str) -> bool: ...

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None:
        """Tear down the current connection and re-establish it.

        Called when is_connected() returns False or a watchdog detects the
        channel is unhealthy.  Implementations should clean up existing state
        before calling connect() again.
        """
        ...

    async def fetch_inbound_since(
        self, channel_jid: str, since: str
    ) -> list[NewMessage]:
        """Fetch messages from channel API newer than ``since``.

        Channels without server-side history (e.g. TUI, WhatsApp) return [].
        The reconciler resolves JIDs before calling — ``channel_jid`` is
        channel-native (e.g. ``slack:C123``).
        """
        ...

    # Optional: typing indicator. Channels that support it implement it.
    # set_typing is NOT part of the protocol — check with hasattr at call sites.

    # Optional: group creation. Not all channels support this.
    # create_group is NOT part of the protocol — check with hasattr at call sites.

    # Whether to prefix outbound messages with the assistant name.
    # Some channels (e.g. Telegram bots) already display their name, so they return false.
    # Default true if not implemented.
    # prefix_assistant_name is NOT part of the protocol — use getattr with default.

    # Optional: streaming message updates. Channels that support it implement:
    #   post_message(jid, text) -> str | None   (returns message ID)
    #   update_message(jid, message_id, text)   (updates in-place)
    # Used by output_handler for real-time text streaming with a cursor indicator.
