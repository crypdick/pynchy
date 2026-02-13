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
    non_main_read_only: bool = True


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
class RegisteredGroup:
    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool | None = None  # Default: True for groups, False for solo


@dataclass
class NewMessage:
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool | None = None


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
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    plugin_mcp_servers: dict[str, dict] | None = None


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

    # Whether to prefix outbound messages with the assistant name.
    # Telegram bots already display their name, so they return false.
    # WhatsApp returns true. Default true if not implemented.
    # prefix_assistant_name is NOT part of the protocol — use getattr with default.


# Callback types
OnInboundMessage = Callable[[str, NewMessage], None]
OnChatMetadata = Callable[[str, str, str | None], None]
