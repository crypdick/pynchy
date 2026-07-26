"""Data models for Pynchy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any, Literal, NewType, Protocol, runtime_checkable

from pynchy.deployment_types import (  # noqa: TC001, RUF100 - public runtime re-export
    DeployChangeKind,
    DeployRevision,
)
from pynchy.session_policy import SessionPolicy  # noqa: TC001, RUF100 - public runtime re-export

if TYPE_CHECKING:
    from pynchy.host.orchestrator.messaging.formatters.base import Formatter

_CONTAINER_TIMEOUT_ERROR = "container_config.timeout: expected number, got {type_name}"
_CONTAINER_MOUNTS_ERROR = "container_config.additional_mounts: expected list, got {type_name}"


# --- Domain identity types ---
#
# Distinct types for the string-shaped identities that thread through the state
# layer and the plugin Protocols (see CONVENTIONS.md "Semantic types for domain
# concepts"). Each is a zero-cost NewType over str: assignable wherever a plain
# str is expected, but passing e.g. a SessionId where a GroupFolder is wanted is
# a mypy error rather than the silent state corruption it is today. Applied at
# boundaries — state-layer signatures and Protocol methods — where a positional
# swap between same-shaped arguments is most costly.
GroupFolder = NewType("GroupFolder", str)  # workspace identity (folder under groups/)
RuntimeId = NewType("RuntimeId", str)  # stable execution identity across control rebinding
SessionId = NewType("SessionId", str)  # agent session handle
ChatJid = NewType("ChatJid", str)  # canonical chat identifier
ChannelName = NewType("ChannelName", str)  # channel instance name (e.g. "slack")


@dataclass(frozen=True)
class DeploymentState:
    """Canonical effective revisions applied by and pending for the host."""

    applied: DeployRevision | None
    pending: DeployRevision | None


class DeployClaimStatus(StrEnum):
    """Outcome of atomically admitting a requested deployment."""

    CLAIMED = "claimed"
    ALREADY_APPLIED = "already_applied"
    ALREADY_PENDING = "already_pending"
    BUSY = "busy"


@dataclass(frozen=True)
class DeployClaim:
    """Deploy admission result with a cause only when work was claimed."""

    status: DeployClaimStatus
    change_kind: DeployChangeKind | None = None


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
    timeout: int | float | None = None  # Seconds (default: 300)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ContainerConfig:
        # Type-check the two known fields at this boundary so a malformed
        # persisted container_config fails here rather than surfacing later as
        # a broken timeout calculation or an opaque mount error.
        timeout = raw.get("timeout")
        if timeout is not None and not isinstance(timeout, int | float):
            raise TypeError(_CONTAINER_TIMEOUT_ERROR.format(type_name=type(timeout).__name__))
        mounts = raw.get("additional_mounts", [])
        if not isinstance(mounts, list):
            raise TypeError(_CONTAINER_MOUNTS_ERROR.format(type_name=type(mounts).__name__))
        return cls(
            additional_mounts=[AdditionalMount(**m) for m in mounts],
            timeout=timeout,
        )


# Tri-state: False (safe), True (risky/gated), "forbidden" (blocked)
TrustLevel = Literal["forbidden"] | bool
CapabilityDecision = Literal["allow", "deny", "needs_human"]


@dataclass
class CapabilityRule:
    """Explicit policy for a semantic capability such as an MCP tool call."""

    decision: CapabilityDecision


# NOTE: Update docs/architecture/security.md § 5 (Service Trust Policy) and
# docs/usage/security.md (Four Properties Per Service) if you change these
# properties or their defaults — both restate this model in prose.
@dataclass(frozen=True)
class ServiceTrustConfig:
    """Four trust properties per service — the user-facing security model.

    Each property answers an intuitive question:
      public_source:    Can untrusted parties provide input through this?
      secret_data:      Does this hold sensitive/secret information?
      public_sink:      Can data I send here reach untrusted parties?
      dangerous_writes: Are writes high-stakes or irreversible?

    Defaults are maximally cautious (all True). Users set False for
    dimensions that don't apply. "forbidden" blocks the capability entirely.
    """

    public_source: TrustLevel = True
    secret_data: bool = True  # True/False only — "forbidden" doesn't apply
    public_sink: TrustLevel = True
    dangerous_writes: TrustLevel = True


@dataclass
class WorkspaceSecurity:
    """Security configuration for a workspace.

    Holds per-service trust declarations, Cop activation, and a flag
    for whether the workspace's local filesystem contains secrets.
    """

    services: dict[str, ServiceTrustConfig] = field(default_factory=dict)
    contains_secrets: bool = False
    cop_active: bool = True
    capabilities: dict[str, CapabilityRule] = field(default_factory=dict)


@dataclass
class WorkspaceProfile:
    """Complete workspace configuration with security profile."""

    # Identity
    jid: str  # Canonical chat identifier
    name: str  # Display name
    folder: str  # Folder under groups/

    # Communication
    trigger: str  # @mention to activate (e.g., "@Pynchy")

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
        if not self.name:
            errors.append("Workspace name is required")
        if not self.folder:
            errors.append("Workspace folder is required")
        if not self.trigger:
            errors.append("Workspace trigger is required")
        return errors


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
    metadata: dict[str, Any] | None = None


class InFlightWorkKind(StrEnum):
    """Agent work that can be resumed semantically after process loss."""

    INTERACTIVE = "interactive"
    RESET_HANDOFF = "reset_handoff"
    SCHEDULED = "scheduled"


class CheckpointControlState(StrEnum):
    """Durable human control over one unfinished agent checkpoint."""

    ACTIVE = "active"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESET_REQUESTED = "reset_requested"


class WorkItemExecutionStatus(StrEnum):
    """Pynchy's durable lifecycle for one linked Linear work item."""

    CLAIMING = "claiming"
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    FOLLOW_UPS = "follow_ups"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    HANDED_OFF = "handed_off"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        """Return whether this status must continue to exclude a second claim."""
        return self in {self.CLAIMING, self.IN_PROGRESS, self.UNKNOWN}

    @property
    def is_explicit_lifecycle_outcome(self) -> bool:
        """Return whether the agent recorded an outcome in Linear's lifecycle."""
        return not self.is_active and self is not self.FAILED


@dataclass(frozen=True)
class WorkItemExecution:
    """Durable link between a Linear issue and one Pynchy execution attempt."""

    id: str
    workspace: str
    linear_issue_id: str
    linear_issue_identifier: str
    linear_issue_url: str
    turn_id: str | None
    task_id: str | None
    attempt: int
    flow_id: str | None
    temporal_workflow_id: str | None
    initiated_by: str
    observed_state_id: str
    observed_state_name: str
    observed_updated_at: str | None
    status: WorkItemExecutionStatus
    summary: str | None
    blocker: str | None
    handoff_to: str | None
    evidence_refs: tuple[str, ...]
    requester_delivery_status: str
    requester_delivery_error: str | None
    requester_delivered_at: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class WorkItemTransitionStatus(StrEnum):
    """Evidence status for a provider transition requested by an execution."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class WorkItemTransition:
    """Intended Linear state change and its provider receipt or uncertainty."""

    id: int
    execution_id: str
    request_id: str
    operation: str
    target_status: str
    result_execution_status: WorkItemExecutionStatus
    evidence_refs: tuple[str, ...]
    status: WorkItemTransitionStatus
    receipt: dict[str, Any] | None
    error: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class InFlightTurn:
    """Durable checkpoint for one agent invocation that has not finalized."""

    turn_id: str
    chat_jid: str
    group_folder: str
    work_kind: InFlightWorkKind
    input_messages: list[dict[str, Any]]
    input_start_cursor: str
    input_end_cursor: str
    started_at: str
    task_id: str | None = None
    session_id: str | None = None
    output_sent: bool = False
    interrupted_at: str | None = None
    deploy_id: str | None = None
    claimed_at: str | None = None
    conversation_claim_id: str | None = None
    input_source: str = "user"
    control_state: CheckpointControlState = CheckpointControlState.ACTIVE


@dataclass
class InboundFetchResult:
    """Result of fetching inbound messages from a channel.

    ``high_water_mark`` is the ISO timestamp of the newest raw message
    seen in the API response (including bot messages that were filtered).
    The reconciler uses it to advance its cursor past bot-only windows
    even when no user messages are found.
    """

    messages: list[NewMessage]
    high_water_mark: str = ""


@dataclass
class ScheduledTask:
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    session_policy: SessionPolicy
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed", "cancelled"] = "active"
    created_at: str = ""
    repo_access: str | None = None  # GitHub slug (owner/repo); None = no worktree
    input_source: str = "scheduled_task"
    config_job_name: str | None = None
    derived_thread_name: str | None = None
    bound_chat_jid: str | None = None
    bound_group_folder: str | None = None
    conversation_id: str | None = None
    last_reset_occurrence: str | None = None

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
            "next_run": None,
        }


@dataclass
class TaskRunLog:
    """Attempt evidence linked across Temporal retries and Pynchy recovery.

    A workflow run ID groups activity retries. A turn ID bridges that workflow
    run to a later interrupted-turn workflow without relying on timestamps.
    """

    task_id: str
    run_at: str
    duration_ms: int | float
    status: Literal["success", "incomplete", "error", "resumed"]
    result: str | None = None
    error: str | None = None
    temporal_workflow_id: str | None = None
    temporal_workflow_run_id: str | None = None
    temporal_attempt: int | None = None
    turn_id: str | None = None
    error_signature: str | None = None
    escalation_reason: str | None = None


class CanaryOutcome(StrEnum):
    """Terminal outcome recorded for one external-service canary run."""

    PASSED = "passed"
    FAILED = "failed"
    CLEANUP_FAILED = "cleanup_failed"
    SKIPPED = "skipped"
    NOT_ESTABLISHED = "not_established"


@dataclass(frozen=True)
class CanaryRun:
    """Durable evidence from one declared external-service canary scenario."""

    run_id: str
    scenario_id: str
    action_ids: tuple[str, ...]
    target_profile: str
    code_revision: str
    config_revision: str
    started_at: str
    completed_at: str
    outcome: CanaryOutcome
    error_class: str | None = None
    evidence_refs: tuple[str, ...] = ()
    is_regression: bool = False
    starts_regression: bool = False
    is_recovery: bool = False


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
            "next_run": None,
        }


@dataclass
class ContainerInput:
    messages: list[dict[str, Any]]  # SDK message list with message types
    group_folder: str
    chat_jid: str
    is_admin: bool
    turn_id: str | None = None
    query_id: str | None = None
    session_id: str | None = None
    is_scheduled_task: bool = False
    input_source: str = "user"
    corruption_tainted: bool = False
    secret_tainted: bool = False
    system_notices: list[str] | None = None
    repo_access: str | None = None  # Primary cwd repo slug; None = no worktree
    repo_accesses: list[str] = field(default_factory=list)  # All mounted repo slugs
    agent_core_module: str = "agent_runner.cores.openai"  # Module path for agent core
    agent_core_class: str = "OpenAIAgentCore"  # Class name for agent core
    agent_core_config: dict[str, Any] | None = None  # Core-specific settings
    plugin_hooks: list[dict[str, str]] = field(default_factory=list)
    system_prompt_append: str | None = None  # Resolved prompts for agent system prompt
    invocation_ts: float = 0.0  # Monotonic timestamp of container spawn (for SecurityGate keying)
    mcp_gateway_url: str | None = None  # LiteLLM MCP gateway URL (SSE transport)
    mcp_gateway_key: str | None = None  # LiteLLM virtual key for workspace's MCP team
    # Direct MCP server connections (bypass LiteLLM gateway).
    # Each entry: {"name": str, "url": str, "transport": "sse"|"http"}
    mcp_direct_servers: list[dict[str, Any]] | None = None


@dataclass
class ContainerOutput:
    status: Literal["success", "error"]
    result: str | None = None
    new_session_id: str | None = None
    error: str | None = None
    type: str = "result"
    thinking: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    text: str | None = None
    # Transparent token stream fields
    system_subtype: str | None = None
    system_data: dict[str, Any] | None = None
    tool_result_id: str | None = None
    tool_result_content: str | None = None
    tool_result_is_error: bool | None = None
    result_metadata: dict[str, Any] | None = None
    query_id: str | None = None


class OutboundEventType(Enum):
    """Types of events flowing from the agent to the channel."""

    TEXT = "text"
    TOOL_TRACE = "tool_trace"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    SYSTEM = "system"
    HOST = "host"
    RESULT = "result"
    APPROVAL = "approval"


@dataclass
class OutboundEvent:
    """A structured event flowing from the agent/host toward the channel.

    Carries both the content and the metadata needed for rich formatting
    (e.g. Slack blocks).
    """

    type: OutboundEventType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


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
    formatter: Formatter

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        """Send a rendered event to the channel.

        This is THE protocol method for all outbound messages.
        Channels call self.formatter.render(event) and send via
        their internal transport.
        """
        ...

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

    def prepare_shutdown(self) -> None:
        """Signal imminent shutdown — suppress reconnect attempts.

        Called early in the shutdown sequence.  The channel should remain
        connected for message delivery but must not attempt reconnection
        if the underlying transport drops.
        """
        ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        """Fetch messages from channel API newer than ``since``.

        Channels without server-side history (e.g. WhatsApp) return
        an empty result.  The reconciler resolves JIDs before calling —
        ``channel_jid`` is channel-native (e.g. ``slack:C123``).
        """
        ...

    # Optional: typing indicator. Channels that support it implement it.
    # set_typing is NOT part of the protocol — check with hasattr at call sites.

    # Optional: group creation. Not all channels support this.
    # create_group is NOT part of the protocol — check with hasattr at call sites.

    # Optional: child-thread creation. Not all channels support this.
    # create_thread is NOT part of the protocol — check with hasattr at call sites.

    # Optional: child-thread lifecycle. Channels may map closed state to their
    # native archive or equivalent through set_thread_closed.

    # Whether to prefix outbound messages with the assistant name.
    # Some channels (e.g. Telegram bots) already display their name, so they return false.
    # Default true if not implemented.
    # prefix_assistant_name is NOT part of the protocol — use getattr with default.

    # Optional streaming message updates: channels can post an event, return a
    # message id, then update that message in place.
    # Used for real-time text streaming and consecutive tool-trace coalescing.
