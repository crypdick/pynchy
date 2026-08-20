"""Shared dependency contract for inbound routing and message processing."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves approval callback annotations.
    Awaitable,
    Callable,
    Coroutine,
)
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves protocol annotations.
from typing import Any, Protocol, TypeVar, runtime_checkable

from pynchy.agent_protocol.api import (  # noqa: TC001 - beartype resolves messaging dependency annotations at runtime.
    ContainerOutput,
    OnOutput,
)
from pynchy.event_bus import Event  # noqa: TC001 - beartype resolves protocol annotations.
from pynchy.identifiers import (
    GroupFolder,  # noqa: TC001 - beartype resolves protocol annotations.
    RuntimeId,  # noqa: TC001 - beartype resolves messaging dependency annotations at runtime.
)
from pynchy.learning_packets import (
    LearningPacket,  # noqa: TC001 - beartype resolves messaging dependency annotations at runtime.
)
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves messaging dependency annotations at runtime.
    Channel,
    NewMessage,
    OutboundEvent,
)
from pynchy.turn_outcomes import (
    TurnOutcome,  # noqa: TC001 - beartype resolves messaging dependency annotations at runtime.
)
from pynchy.workspace.api import (  # beartype resolves messaging dependency annotations at runtime.
    RuntimeTarget,
    WorkspaceProfile,
)

type Group = WorkspaceProfile
_QueueResultT = TypeVar("_QueueResultT")


@dataclass(frozen=True)
class CommandWords:
    """One resolved command vocabulary, normalized for matching."""

    verbs: frozenset[str]
    nouns: frozenset[str]
    aliases: frozenset[str]


@dataclass(frozen=True)
class CommandMatcher:
    """The command values the application supplies to message processing."""

    trigger_pattern: object
    reset: CommandWords
    end_session: CommandWords
    redeploy: CommandWords
    pause: CommandWords

    @classmethod
    def from_values(
        cls,
        trigger_pattern: object,
        values: dict[str, dict[str, list[str]]],
    ) -> CommandMatcher:
        def words(name: str) -> CommandWords:
            value = values[name]
            return CommandWords(
                frozenset(value.get("verbs", [])),
                frozenset(value.get("nouns", [])),
                frozenset(value.get("aliases", [])),
            )

        return cls(
            trigger_pattern=trigger_pattern,
            reset=words("reset"),
            end_session=words("end_session"),
            redeploy=words("redeploy"),
            pause=words("pause"),
        )


@dataclass(frozen=True)
class DirectCommandOutput:
    """One persisted result from a trusted direct host command."""

    chat_jid: str
    group: Group
    source_message: NewMessage
    command: str
    exit_code: int | None
    content: str
    timestamp: str


@dataclass
class ApprovalRuntimeOperations:
    """Approval storage and replay selected by the application composition root."""

    find_pending_by_short_id: Callable[[str], dict[str, Any] | None]
    list_pending_approvals: Callable[[], list[dict[str, Any]]]
    persist_and_process: Callable[[str, dict[str, object]], Awaitable[None]]


@runtime_checkable
class DirectCommandDeps(Protocol):
    """Narrow runtime capabilities required for direct host commands."""

    def direct_command_workdir(self, group: Group) -> Path: ...

    async def record_direct_command_output(self, output: DirectCommandOutput) -> None: ...

    async def broadcast_to_channels(
        self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def emit(self, event: Event) -> None: ...


@runtime_checkable
class MessageQueue(Protocol):
    """Queue operations used across inbound routing and turn control."""

    def is_active_task(self, runtime_id: RuntimeId) -> bool: ...

    def has_active_run(self, runtime_id: RuntimeId) -> bool: ...

    def send_message(self, runtime_id: RuntimeId, content: str) -> bool: ...

    def enqueue_message_check(self, target: RuntimeTarget) -> None: ...

    def defer_interrupt_until_tool_result(self, runtime_id: RuntimeId) -> None: ...

    def clear_pending_tasks(self, runtime_id: RuntimeId) -> tuple[str, ...]: ...

    async def stop_active_process(self, runtime_id: RuntimeId) -> None: ...

    async def stop_active_process_for_control(self, runtime_id: RuntimeId) -> None: ...

    async def destroy_runtime_session(self, runtime_id: RuntimeId) -> None: ...

    async def interrupt_after_tool_result(self, runtime_id: RuntimeId) -> bool: ...

    async def run_message_turn(self, target: RuntimeTarget) -> TurnOutcome: ...

    async def run_serialized_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        fn: Callable[[], Awaitable[_QueueResultT]],
    ) -> _QueueResultT: ...


@runtime_checkable
class MessageHandlerDeps(DirectCommandDeps, Protocol):
    """Dependencies shared by polling and the interactive processing pipeline."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def command_matcher(self) -> CommandMatcher: ...

    @property
    def approval_runtime_operations(self) -> ApprovalRuntimeOperations: ...

    @property
    def workspaces(self) -> dict[str, Group]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    last_timestamp: str

    agent_name: str
    message_poll_interval: float

    @property
    def message_data_dir(self) -> Path: ...

    def filter_allowed_messages(
        self,
        messages: list[NewMessage],
        group: Group,
        channel_plugin_name: str | None,
    ) -> list[NewMessage]: ...

    def linear_workspace_enabled(self, group: Group) -> bool: ...

    async def create_linear_workspace_todo(
        self, group: Group, title: str
    ) -> dict[str, object] | None: ...

    @property
    def queue(self) -> MessageQueue: ...

    async def save_state(self) -> None: ...

    def routing_cursor(self, chat_jid: str) -> str: ...

    def mark_dispatched(self, chat_jid: str, timestamp: str) -> None: ...

    def pop_dispatched(self, chat_jid: str, default: str) -> str: ...

    async def handle_context_reset(
        self,
        chat_jid: str,
        group: Group,
        timestamp: str,
        *,
        source_message: NewMessage | None = None,
    ) -> None: ...

    async def handle_end_session(
        self,
        chat_jid: str,
        group: Group,
        timestamp: str,
        *,
        source_message: NewMessage | None = None,
    ) -> None: ...

    async def trigger_manual_redeploy(
        self,
        chat_jid: str,
        *,
        source_message: NewMessage | None = None,
    ) -> None: ...

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None: ...

    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None: ...

    def register_idle_callback(
        self, group_folder: GroupFolder, callback: Callable[[], Coroutine[Any, Any, None]]
    ) -> None: ...

    def processing_ack_emoji(self, chat_jid: str) -> str | None: ...

    def repo_is_dirty(self) -> bool: ...

    def new_learning_run_summary(self) -> object: ...

    def observe_learning_output(self, summary: object, output: ContainerOutput) -> None: ...

    async def set_typing_on_channels(self, chat_jid: str, *, is_typing: bool) -> None: ...

    async def catch_up_channels(self) -> None: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...

    async def start_interrupted_turn(self, turn_id: str, group_folder: str) -> None: ...

    async def start_learning_review_workflow(self, packet: LearningPacket) -> None: ...

    async def start_completed_turn_learning_review(
        self,
        chat_jid: str,
        group: Group,
        messages: list[NewMessage],
        final_cursor: str,
        summary: object,
    ) -> None: ...

    async def run_agent(  # noqa: PLR0913 - protocol preserves orchestration call shape.
        self,
        group: Group,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        input_source: str = "user",
        turn_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> str: ...

    async def handle_streamed_output(
        self,
        chat_jid: str,
        group: Group,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool: ...
