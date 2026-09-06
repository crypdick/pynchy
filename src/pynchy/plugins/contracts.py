"""Typed contribution objects for Pynchy plugin hooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import (
    Path,  # beartype resolves dataclass annotations at runtime.
)
from typing import Any, Literal, Protocol, runtime_checkable

from pynchy.plugins.mcp_server import (
    McpServerConfig,
)
from pynchy.workspace.api import (
    ServiceTrustConfig,
)

SUPPORTED_AUDIO_SUFFIXES = frozenset(
    {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}
)


@dataclass(frozen=True)
class AudioTranscriptionResult:
    success: bool
    transcript: str = ""
    provider: str = "none"
    model: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class InboundAudioAttachment:
    id: str
    filename: str
    content_type: str | None
    size: int | None
    data: bytes | None


@dataclass(frozen=True)
class AudioMetadataPatch:
    index: int
    cached_path: str | None
    transcription: dict[str, Any]


@dataclass(frozen=True)
class InboundAudioProcessingResult:
    content: str
    metadata_patches: tuple[AudioMetadataPatch, ...] = ()


@dataclass(frozen=True)
class InboundAudioProcessingRequest:
    attachments: tuple[InboundAudioAttachment, ...]
    content: str
    fallback_content: str
    cache_dir: Path
    message_id: str


def is_supported_audio_filename(filename: str) -> bool:
    """Return whether a filename denotes a supported audio payload."""
    return Path(filename).suffix.lower() in SUPPORTED_AUDIO_SUFFIXES


@runtime_checkable
class RuntimeProvider(Protocol):
    """Container runtime capability supplied by a plugin adapter."""

    name: str
    cli: str

    def is_available(self) -> bool: ...
    def ensure_running(self) -> None: ...
    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]: ...


@dataclass
class NewMessage:
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool | None = None
    message_type: str = "user"
    metadata: dict[str, Any] | None = None
    local_sequence: int | None = None


class OutboundEventType(Enum):
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
    type: OutboundEventType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InboundFetchResult:
    messages: list[NewMessage]
    high_water_mark: str = ""


@dataclass
class PluginVerification:
    plugin_name: str
    git_sha: str  # noqa: V107
    verified_at: str  # noqa: V107
    verdict: Literal["pass", "fail"]
    reasoning: str  # noqa: V107
    model: str


@runtime_checkable
class ChannelFormatter(Protocol):
    def render(self, event: OutboundEvent) -> object: ...


@runtime_checkable
class Channel(Protocol):
    name: str
    formatter: ChannelFormatter

    async def connect(self) -> None: ...
    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...
    async def reconnect(self) -> None: ...
    def prepare_shutdown(self) -> None: ...
    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult: ...


@dataclass(frozen=True, slots=True)
class AgentCoreSpec:
    """One agent-core implementation available to the host and runner."""

    name: str
    module: str
    class_name: str
    packages: tuple[str, ...] = ()
    host_source_path: Path | None = None  # noqa: V107


@dataclass(frozen=True, slots=True)
class AgentHookSpec:
    """One trusted agent lifecycle hook module supplied by a plugin."""

    name: str
    module_path: Path


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One named, validated MCP server template supplied by a plugin."""

    name: str
    config: McpServerConfig
    trust: ServiceTrustConfig | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """One named, validated workspace supplied by a plugin."""

    folder: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One named, validated config-backed job supplied by a plugin."""

    name: str
    config: dict[str, object]
