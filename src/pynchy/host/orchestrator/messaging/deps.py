"""Shared dependency contract for inbound routing and message processing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pynchy.types as types
from pynchy.event_bus import Event  # noqa: TC001, RUF100 - beartype resolves protocol annotations.

if TYPE_CHECKING:
    from pynchy.host.container_manager import OnOutput
    from pynchy.host.orchestrator.concurrency import GroupQueue

type Group = types.WorkspaceProfile


@runtime_checkable
class MessageHandlerDeps(Protocol):
    """Dependencies shared by polling and the interactive processing pipeline."""

    @property
    def channels(self) -> list[types.Channel]: ...

    @property
    def workspaces(self) -> dict[str, Group]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    last_timestamp: str

    @property
    def queue(self) -> GroupQueue: ...

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
        source_message: types.NewMessage | None = None,
    ) -> None: ...

    async def handle_end_session(
        self,
        chat_jid: str,
        group: Group,
        timestamp: str,
        *,
        source_message: types.NewMessage | None = None,
    ) -> None: ...

    async def trigger_manual_redeploy(
        self,
        chat_jid: str,
        *,
        source_message: types.NewMessage | None = None,
    ) -> None: ...

    async def broadcast_to_channels(
        self, chat_jid: str, event: types.OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None: ...

    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None: ...

    def processing_ack_emoji(self, chat_jid: str) -> str | None: ...

    async def set_typing_on_channels(self, chat_jid: str, *, is_typing: bool) -> None: ...

    async def catch_up_channels(self) -> None: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...

    async def start_interrupted_turn(self, turn_id: str, group_folder: str) -> None: ...

    def emit(self, event: Event) -> None: ...

    async def run_agent(  # noqa: PLR0913, RUF100 - protocol preserves orchestration call shape.
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
        result: types.ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool: ...
