"""Data models for Pynchy.

Port of src/types.ts — interfaces become dataclasses, Channel becomes Protocol.
"""

from __future__ import annotations

from collections.abc import Callable
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
    non_god_read_only: bool = True


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

    risk_tier: Literal["read-only", "policy-check", "human-approval"]
    enabled: bool = True


@dataclass
class WorkspaceSecurity:
    """Security configuration for a workspace."""

    # MCP tool permissions (tool_name -> config)
    mcp_tools: dict[str, McpToolConfig] = field(default_factory=dict)

    # Default risk tier for tools not explicitly configured
    default_risk_tier: Literal["read-only", "policy-check", "human-approval"] = "human-approval"

    # Filesystem and network access
    allow_filesystem_access: bool = True
    allow_network_access: bool = True


@dataclass
class WorkspaceProfile:
    """Complete workspace configuration with security profile.

    Replaces RegisteredGroup with added security features for Phase B.1
    of security hardening.
    """

    # Identity
    jid: str  # WhatsApp JID or workspace identifier
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
    is_god: bool = False

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
        valid_tiers = {"read-only", "policy-check", "human-approval"}
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

        return errors

    @classmethod
    def from_registered_group(cls, jid: str, rg: RegisteredGroup) -> WorkspaceProfile:
        """Migrate from old RegisteredGroup format.

        Args:
            jid: The workspace JID
            rg: RegisteredGroup instance to migrate

        Returns:
            WorkspaceProfile with default security settings
        """
        return cls(
            jid=jid,
            name=rg.name,
            folder=rg.folder,
            trigger=rg.trigger,
            requires_trigger=rg.requires_trigger if rg.requires_trigger is not None else True,
            container_config=rg.container_config,
            security=WorkspaceSecurity(),  # Default security profile
            is_god=rg.is_god,
            added_at=rg.added_at,
        )

    def to_registered_group(self) -> RegisteredGroup:
        """Convert to legacy RegisteredGroup format for backward compatibility.

        Returns:
            RegisteredGroup instance (security info is lost)
        """
        return RegisteredGroup(
            name=self.name,
            folder=self.folder,
            trigger=self.trigger,
            added_at=self.added_at,
            container_config=self.container_config,
            requires_trigger=self.requires_trigger,
            is_god=self.is_god,
        )


@dataclass
class RegisteredGroup:
    """Legacy group configuration format.

    DEPRECATED: Use WorkspaceProfile instead.
    Kept for backward compatibility during migration.
    """

    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool | None = None  # Default: True for groups, False for solo
    is_god: bool = False


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
    project_access: bool = False

    def to_snapshot_dict(self) -> dict[str, str | None]:
        """Serialize to the dict format expected by write_tasks_snapshot.

        Used by both app.py and task_scheduler.py to avoid duplicating the
        field mapping when building the tasks snapshot for containers.
        """
        return {
            "id": self.id,
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
class ContainerInput:
    messages: list[dict]  # SDK message list with message types
    group_folder: str
    chat_jid: str
    is_god: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    plugin_mcp_servers: dict[str, dict] | None = None
    system_notices: list[str] | None = None
    project_access: bool = False
    agent_core_module: str = "agent_runner.cores.claude"  # Module path for agent core
    agent_core_class: str = "ClaudeAgentCore"  # Class name for agent core
    agent_core_config: dict | None = None  # Core-specific settings


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

    def is_connected(self) -> bool: ...

    def owns_jid(self, jid: str) -> bool: ...

    async def disconnect(self) -> None: ...

    # Optional: typing indicator. Channels that support it implement it.
    # set_typing is NOT part of the protocol — check with hasattr at call sites.

    # Optional: group creation. Only WhatsApp supports this currently.
    # create_group is NOT part of the protocol — check with hasattr at call sites.

    # Whether to prefix outbound messages with the assistant name.
    # Telegram bots already display their name, so they return false.
    # WhatsApp returns true. Default true if not implemented.
    # prefix_assistant_name is NOT part of the protocol — use getattr with default.


# Callback types
OnInboundMessage = Callable[[str, NewMessage], None]
OnChatMetadata = Callable[[str, str, str | None], None]
