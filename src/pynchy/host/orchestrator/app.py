"""Main orchestrator — owns runtime state and wires subsystems together.

Lifecycle (startup phases, shutdown) lives in :mod:`lifecycle`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

    from pynchy.host.container_manager import OnOutput

from pynchy.host.orchestrator import session_handler
from pynchy.host.orchestrator.adapters import HostMessageBroadcaster, MessageBroadcaster
from pynchy.chat import (
    channel_handler,
    message_handler,
    output_handler,
)
from pynchy.config import get_settings
from pynchy.state import (
    delete_workspace_profile,
    get_all_chats,
    get_all_sessions,
    get_all_workspace_profiles,
    get_router_state,
    set_workspace_profile,
)
from pynchy.event_bus import EventBus
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.logger import logger
from pynchy.types import (
    Channel,
    ContainerOutput,
    NewMessage,
    WorkspaceProfile,
)


class PynchyApp:
    """Main application class — owns all runtime state and wires subsystems."""

    def __init__(self) -> None:
        self.last_timestamp: str = ""
        self.sessions: dict[str, str] = {}
        self._session_cleared: set[str] = set()  # group folders with pending clears
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
        self._http_runner: Any | None = None
        self._observers: list[Any] = []
        self._memory: Any | None = None
        self._subsystem_tasks: list[asyncio.Task[None]] = []
        self.plugin_manager: pluggy.PluginManager | None = None

        # Shared broadcast infrastructure — single code path for all channel sends.
        # Uses lambda so broadcaster always reads current self.channels reference.
        self._broadcaster = MessageBroadcaster(
            lambda: self.channels, workspaces=lambda: self.workspaces
        )
        self._host_broadcaster = self._make_host_broadcaster()

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

    async def _save_state(self) -> None:
        """Persist router state to the database atomically.

        Both rows are written in a single transaction so a crash can never
        leave them inconsistent.
        """
        from pynchy.state import save_router_state_batch

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

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict], *, source: str = "user"
    ) -> None:
        await output_handler.broadcast_agent_input(self, chat_jid, messages, source=source)

    async def run_agent(
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
    ) -> str:
        from pynchy.host.orchestrator import agent_runner

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

    def emit(self, event: Any) -> None:
        self.event_bus.emit(event)

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
    ) -> None:
        await self._broadcaster._broadcast_to_channels(
            chat_jid, text, suppress_errors=suppress_errors
        )

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None:
        await channel_handler.send_reaction_to_channels(self, chat_jid, message_id, sender, emoji)

    async def set_typing_on_channels(self, chat_jid: str, is_typing: bool) -> None:
        await channel_handler.set_typing_on_channels(self, chat_jid, is_typing)

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        await self._host_broadcaster.broadcast_host_message(chat_jid, text)

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        await self._host_broadcaster.broadcast_system_notice(chat_jid, text)

    def _make_host_broadcaster(self) -> HostMessageBroadcaster:
        """Create a HostMessageBroadcaster wired to this app's store and event bus."""
        from pynchy.state import store_message_direct

        async def store_host_message(**kwargs: Any) -> None:
            await store_message_direct(**kwargs, message_type="host")

        async def store_system_notice(**kwargs: Any) -> None:
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
        """Register a new workspace and persist it."""
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

    async def _unregister_workspace(self, jid: str) -> None:
        """Remove an orphaned workspace registration."""
        self.workspaces.pop(jid, None)
        await delete_workspace_profile(jid)

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

    # ------------------------------------------------------------------
    # Internal delegation for session_handler (used by dep_factory adapters)
    async def _ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None:
        await session_handler.ingest_user_message(self, msg, source_channel=source_channel)

    async def _on_inbound(self, _jid: str, msg: NewMessage) -> None:
        await session_handler.on_inbound(self, _jid, msg)

    async def _on_reaction(self, jid: str, message_ts: str, user_id: str, emoji: str) -> None:
        """Handle an inbound reaction from a channel."""
        from pynchy.chat.reaction_handler import handle_reaction

        await handle_reaction(self, jid, message_ts, user_id, emoji)

    async def _on_ask_user_answer(self, request_id: str, answer: dict) -> None:
        """Handle an ask_user answer from a channel interaction callback."""
        from pynchy.chat.ask_user_handler import handle_ask_user_answer

        await handle_ask_user_answer(request_id, answer, self)

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
        import uuid
        from datetime import UTC, datetime

        from pynchy.state import store_message

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
        self.queue.enqueue_message_check(chat_jid)

    async def _send_clear_confirmation(self, chat_jid: str) -> None:
        await session_handler.send_clear_confirmation(self, chat_jid)

    # ------------------------------------------------------------------
    # Channel history catch-up
    # ------------------------------------------------------------------

    async def _catch_up_channel_history(self) -> None:
        """Reconcile channel history and retry pending outbound.

        Delegates to the unified reconciler which handles per-channel
        bidirectional cursors, inbound catch-up, and outbound retry.

        Runs at boot AND periodically from the message polling loop.
        """
        from pynchy.chat.reconciler import reconcile_all_channels

        await reconcile_all_channels(self)

    # ------------------------------------------------------------------
    # Lifecycle (delegated to _lifecycle module)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point — see :func:`pynchy.host.orchestrator.lifecycle.run_app`."""
        from pynchy.host.orchestrator.lifecycle import run_app

        await run_app(self)
