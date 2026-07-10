"""Main orchestrator — owns runtime state and wires subsystems together.

Lifecycle (startup phases, shutdown) lives in :mod:`lifecycle`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pluggy  # noqa: TC002, RUF100 - beartype resolves app annotations at runtime.

if TYPE_CHECKING:
    from pynchy.host.container_manager import OnOutput

from pynchy.config import get_settings
from pynchy.event_bus import Event, EventBus
from pynchy.host.container_manager import (  # noqa: TC001, RUF100 - beartype resolves app annotations at runtime.
    OnOutput,
)
from pynchy.host.orchestrator import agent_runner, session_handler
from pynchy.host.orchestrator.adapters import HostMessageBroadcaster, MessageBroadcaster
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.messaging import (
    ask_user_handler,
    channel_handler,
    reaction_handler,
)
from pynchy.host.orchestrator.messaging import (
    pipeline as message_handler,
)
from pynchy.host.orchestrator.messaging import (
    router as output_handler,
)
from pynchy.host.orchestrator.temporal import scheduler as temporal_scheduler
from pynchy.logger import logger
from pynchy.plugins.memory import (  # noqa: TC001, RUF100 - beartype resolves app annotations at runtime.
    MemoryProvider,
)
from pynchy.plugins.observers import (  # noqa: TC001, RUF100 - beartype resolves app annotations at runtime.
    ObserverProvider,
)
from pynchy.state import (
    delete_workspace_profile,
    get_all_chats,
    get_all_sessions,
    get_all_workspace_profiles,
    get_router_state,
    save_router_state_batch,
    set_workspace_profile,
    store_message,
    store_message_direct,
)
from pynchy.types import (
    Channel,
    ContainerOutput,
    NewMessage,
    OutboundEvent,
    WorkspaceProfile,
)


class PynchyApp:
    """Main application class — owns all runtime state and wires subsystems."""

    def __init__(self) -> None:
        self.last_timestamp: str = ""
        self.sessions: dict[str, str] = {}
        self.session_cleared: set[str] = set()  # group folders with pending clears
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.last_agent_timestamp: dict[str, str] = {}
        # Transient dispatch tracker — NOT persisted.  Resets to {} on every
        # restart so recover_pending_messages always uses last_agent_timestamp
        # (the true "successfully processed" cursor) as its baseline.
        self._dispatched_through: dict[str, str] = {}
        self.message_loop_running: bool = False
        self.queue: GroupQueue = GroupQueue()
        self.channels: list[Channel] = []
        self.event_bus: EventBus = EventBus()
        self._shutting_down: bool = False
        self._http_runner: object | None = None
        self._observers: list[ObserverProvider] = []
        self._memory: MemoryProvider | None = None
        self._subsystem_tasks: list[object] = []
        self.plugin_manager: pluggy.PluginManager | None = None

        # Shared broadcast infrastructure — single code path for all channel sends.
        # Uses lambda so broadcaster always reads current self.channels reference.
        self._broadcaster = MessageBroadcaster(
            lambda: self.channels, workspaces=lambda: self.workspaces
        )
        self._host_broadcaster = self._make_host_broadcaster()

    @property
    def message_broadcaster(self) -> MessageBroadcaster:
        """Return the shared raw channel broadcaster."""
        return self._broadcaster

    @property
    def host_broadcaster(self) -> HostMessageBroadcaster:
        """Return the shared host/system notice broadcaster."""
        return self._host_broadcaster

    def is_shutting_down(self) -> bool:
        """Return whether shutdown has started."""
        return self._shutting_down

    def begin_shutdown(self) -> bool:
        """Mark shutdown as started; return False if shutdown was already active."""
        if self._shutting_down:
            return False
        self._shutting_down = True
        return True

    def cancel_subsystem_tasks(self) -> None:
        for task in self._subsystem_tasks:
            cast("Any", task).cancel()
        self._subsystem_tasks.clear()

    def add_subsystem_task(self, task: object) -> None:
        self._subsystem_tasks.append(task)

    async def cleanup_http_runner(self) -> None:
        if self._http_runner is None:
            return
        await cast("Any", self._http_runner).cleanup()

    def set_http_runner(self, runner: object) -> None:
        self._http_runner = runner

    def attach_observers(self, observers: list[ObserverProvider]) -> None:
        self._observers = observers

    async def close_observers(self) -> None:
        for observer in self._observers:
            await observer.close()

    async def set_memory_provider(self, memory: MemoryProvider | None) -> None:
        self._memory = memory
        if self._memory:
            await self._memory.init()

    async def close_memory_provider(self) -> None:
        if self._memory:
            await self._memory.close()

    def routing_cursor(self, chat_jid: str) -> str:
        """Return the cursor for fetching messages during routing."""
        return max(
            self.last_agent_timestamp.get(chat_jid, ""),
            self._dispatched_through.get(chat_jid, ""),
        )

    def mark_dispatched(self, chat_jid: str, timestamp: str) -> None:
        """Record the furthest message timestamp dispatched to an active container."""
        self._dispatched_through[chat_jid] = timestamp

    def pop_dispatched(self, chat_jid: str, default: str) -> str:
        """Return and clear the in-memory dispatched timestamp for a chat."""
        return self._dispatched_through.pop(chat_jid, default)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def _load_state(self) -> None:
        """Load persisted state from the database."""
        self.last_timestamp = await get_router_state("last_timestamp") or ""
        agent_ts = await get_router_state("last_agent_timestamp")
        try:
            self.last_agent_timestamp = json.loads(agent_ts) if agent_ts else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted last_agent_timestamp in DB, resetting")
            self.last_agent_timestamp = {}
        self.sessions = await get_all_sessions()

        self.workspaces = await get_all_workspace_profiles()

        logger.info(
            "State loaded",
            workspace_count=len(self.workspaces),
        )

    async def load_state(self) -> None:
        await self._load_state()

    async def _save_state(self) -> None:
        """Persist router state to the database atomically.

        Both rows are written in a single transaction so a crash can never
        leave them inconsistent.
        """
        await save_router_state_batch(
            {
                "last_timestamp": self.last_timestamp,
                "last_agent_timestamp": json.dumps(self.last_agent_timestamp),
            }
        )

    # ------------------------------------------------------------------
    # Protocol adapter methods (satisfy handler Protocols via structural typing)
    # ------------------------------------------------------------------

    async def save_state(self) -> None:
        await self._save_state()

    async def handle_context_reset(
        self, chat_jid: str, group: WorkspaceProfile, timestamp: str
    ) -> None:
        await session_handler.handle_context_reset(self, chat_jid, group, timestamp)

    async def handle_end_session(
        self, chat_jid: str, group: WorkspaceProfile, timestamp: str
    ) -> None:
        await session_handler.handle_end_session(self, chat_jid, group, timestamp)

    async def trigger_manual_redeploy(self, chat_jid: str) -> None:
        await session_handler.trigger_manual_redeploy(self, chat_jid)

    async def catch_up_channels(self) -> None:
        await self._catch_up_channel_history()

    async def start_channel_reconciliation(self) -> None:
        """Start durable Temporal reconciliation for channel history."""
        await temporal_scheduler.start_channel_reconciliation_workflow()

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None:
        await output_handler.broadcast_agent_input(self, chat_jid, messages, source=source)

    async def run_agent(  # noqa: PLR0913, RUF100 - protocol-facing orchestration entry point keeps the full dependency contract explicit.
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
    ) -> str:
        return await agent_runner.run_agent(
            self,
            group,
            chat_jid,
            messages,
            on_output,
            extra_system_notices,
            is_scheduled_task=is_scheduled_task,
            repo_access_override=repo_access_override,
            input_source=input_source,
        )

    def emit(self, event: Event) -> None:
        self.event_bus.emit(event)

    async def broadcast_to_channels(
        self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None:
        await self._broadcaster.broadcast_to_channels(
            chat_jid, event, suppress_errors=suppress_errors
        )

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None:
        await channel_handler.send_reaction_to_channels(self, chat_jid, message_id, sender, emoji)

    def processing_ack_emoji(self, chat_jid: str) -> str | None:
        return channel_handler.processing_ack_emoji(self, chat_jid)

    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None:
        await channel_handler.send_reaction_to_outbound(self, chat_jid, per_channel_ids, emoji)

    async def set_typing_on_channels(self, chat_jid: str, *, is_typing: bool) -> None:
        await channel_handler.set_typing_on_channels(self, chat_jid, is_typing=is_typing)

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        await self._host_broadcaster.broadcast_host_message(chat_jid, text)

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        await self._host_broadcaster.broadcast_system_notice(chat_jid, text)

    def _make_host_broadcaster(self) -> HostMessageBroadcaster:
        """Create a HostMessageBroadcaster wired to this app's store and event bus."""

        async def store_host_message(**kwargs: object) -> None:
            await store_message_direct(**kwargs, message_type="host")

        async def store_system_notice(**kwargs: object) -> None:
            await store_message_direct(**kwargs, message_type="user")

        return HostMessageBroadcaster(
            self._broadcaster, store_host_message, store_system_notice, self.event_bus.emit
        )

    async def handle_streamed_output(
        self, chat_jid: str, group: WorkspaceProfile, result: ContainerOutput
    ) -> bool:
        return await output_handler.handle_streamed_output(self, chat_jid, group, result)

    # ------------------------------------------------------------------
    # Group management
    # ------------------------------------------------------------------

    async def _register_workspace(self, profile: WorkspaceProfile) -> None:
        """Register a workspace and persist it."""
        self.workspaces[profile.jid] = profile
        await set_workspace_profile(profile)

        workspace_dir = get_settings().groups_dir / profile.folder
        (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

        logger.info(
            "Workspace registered",
            jid=profile.jid,
            name=profile.name,
            folder=profile.folder,
        )

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        """Register a workspace from subsystem adapters."""
        await self._register_workspace(profile)

    async def _unregister_workspace(self, jid: str) -> None:
        """Remove an orphaned workspace registration."""
        self.workspaces.pop(jid, None)
        await delete_workspace_profile(jid)

    async def unregister_workspace(self, jid: str) -> None:
        await self._unregister_workspace(jid)

    async def get_available_groups(self) -> list[dict[str, Any]]:
        """Get available groups list for the agent, ordered by most recent activity."""
        chats = await get_all_chats()
        registered_jids = set(self.workspaces.keys())

        def is_channel_visible(jid: str) -> bool:
            if jid == "__group_sync__":
                return False

            # During startup/tests there may be no channels loaded yet; expose all
            # persisted chats so metadata APIs and snapshots remain available.
            if not self.channels:
                return True

            return any(ch.owns_jid(jid) for ch in self.channels)

        return [
            {
                "jid": c["jid"],
                "name": c["name"],
                "lastActivity": c["last_message_time"],
                "isRegistered": c["jid"] in registered_jids,
            }
            for c in chats
            if is_channel_visible(c["jid"])
        ]

    # ------------------------------------------------------------------
    # Message processing delegation
    # ------------------------------------------------------------------

    async def _process_group_messages(self, chat_jid: str) -> bool:
        """Delegates group processing to the message handler module."""
        return await message_handler.process_group_messages(self, chat_jid)

    async def process_group_messages(self, chat_jid: str) -> bool:
        return await self._process_group_messages(chat_jid)

    async def start_interactive_turn(self, chat_jid: str) -> None:
        """Start durable Temporal processing for pending messages in one chat."""
        await temporal_scheduler.start_interactive_message_workflow(chat_jid)

    # ------------------------------------------------------------------
    # Internal delegation for session_handler (used by dep_factory adapters)
    async def _ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None:
        await session_handler.ingest_user_message(self, msg, source_channel=source_channel)

    async def ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None:
        await self._ingest_user_message(msg, source_channel=source_channel)

    async def _on_inbound(self, _jid: str, msg: NewMessage) -> None:
        await session_handler.on_inbound(self, _jid, msg)

    async def on_inbound(self, jid: str, msg: NewMessage) -> None:
        await self._on_inbound(jid, msg)

    async def _on_reaction(self, jid: str, message_ts: str, user_id: str, emoji: str) -> None:
        """Handle an inbound reaction from a channel."""
        await reaction_handler.handle_reaction(self, jid, message_ts, user_id, emoji)

    async def on_reaction(self, jid: str, message_ts: str, user_id: str, emoji: str) -> None:
        await self._on_reaction(jid, message_ts, user_id, emoji)

    async def _on_ask_user_answer(self, request_id: str, answer: dict[str, Any]) -> None:
        """Handle an ask_user answer from a channel interaction callback."""
        await ask_user_handler.handle_ask_user_answer(request_id, answer, self)

    async def on_ask_user_answer(self, request_id: str, answer: dict[str, Any]) -> None:
        await self._on_ask_user_answer(request_id, answer)

    async def enqueue_message(self, chat_jid: str, text: str) -> None:
        """Inject a synthetic message for cold-start answer delivery.

        Satisfies the AskUserDeps protocol.  Stores the message directly
        and triggers queue processing, bypassing user-message filters
        (allowed_users, trigger patterns) that would reject system messages.

        NOTE: This intentionally uses a direct store_message call with
        is_from_me=False because the LLM polling loop (get_messages_since)
        only returns is_from_me=0 rows.  broadcast_host_message and
        broadcast_system_notice both set is_from_me=True, so they can't
        be used here.  The host message below ensures the user sees what
        was forwarded (token stream transparency).
        """
        msg = NewMessage(
            id=f"ask-user-answer-{uuid.uuid4().hex[:8]}",
            chat_jid=chat_jid,
            sender="system",
            sender_name="System",
            content=text,
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
            message_type="system",
        )
        await store_message(msg)
        await self.broadcast_host_message(chat_jid, "\U0001f60e Answer forwarded to agent")
        await self.start_interactive_turn(chat_jid)

    async def _send_clear_confirmation(self, chat_jid: str) -> None:
        await session_handler.send_clear_confirmation(self, chat_jid)

    async def send_clear_confirmation(self, chat_jid: str) -> None:
        await self._send_clear_confirmation(chat_jid)

    # ------------------------------------------------------------------
    # Channel history catch-up
    # ------------------------------------------------------------------

    async def _catch_up_channel_history(self) -> None:
        """Start Temporal-owned channel history reconciliation."""
        if not temporal_scheduler.temporal_scheduler_runtime_active():
            logger.info("Channel reconciliation deferred until Temporal scheduler runtime starts")
            return
        try:
            await self.start_channel_reconciliation()
        except temporal_scheduler.TemporalRuntimeUnavailableError:
            logger.info("Channel reconciliation deferred until Temporal scheduler runtime starts")
        except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; history catch-up is best-effort startup work.
            logger.warning(
                "Channel reconciliation skipped after startup dispatch failure",
                exc_type=type(exc).__name__,
                err=str(exc),
            )

    # ------------------------------------------------------------------
    # Lifecycle (delegated to _lifecycle module)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point — see :func:`pynchy.host.orchestrator.lifecycle.run_app`."""
        from pynchy.host.orchestrator.lifecycle import (  # noqa: PLC0415, RUF100 - lifecycle imports PynchyApp for runtime annotations.
            run_app,
        )

        await run_app(self)
