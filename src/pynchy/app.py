"""Main orchestrator — wires all subsystems together."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy import (
    session_handler,
    startup_handler,
)
from pynchy.adapters import HostMessageBroadcaster, MessageBroadcaster
from pynchy.config import get_settings
from pynchy.db import (
    get_aliases_for_jid,
    get_all_aliases,
    get_all_chats,
    get_all_sessions,
    get_all_workspace_profiles,
    get_router_state,
    init_database,
    set_jid_alias,
    set_router_state,
    set_workspace_profile,
    store_chat_metadata,
)
from pynchy.event_bus import EventBus
from pynchy.group_queue import GroupQueue
from pynchy.infra.http_server import start_http_server
from pynchy.infra.service_installer import install_service
from pynchy.infra.system_checks import ensure_container_system_running
from pynchy.logger import logger
from pynchy.messaging import (
    channel_handler,
    message_handler,
    output_handler,
)
from pynchy.messaging.channel_runtime import (
    ChannelPluginContext,
    load_channels,
    resolve_default_channel,
)
from pynchy.plugin.verifier import load_verified_plugins, scan_and_install_new_plugins
from pynchy.tunnels import check_tunnels
from pynchy.types import (
    Channel,
    ContainerOutput,
    NewMessage,
    RegisteredGroup,
    WorkspaceProfile,
)


class PynchyApp:
    """Main application class — owns all runtime state and wires subsystems."""

    def __init__(self) -> None:
        self.last_timestamp: str = ""
        self.sessions: dict[str, str] = {}
        self._session_cleared: set[str] = set()  # group folders with pending clears
        self.workspaces: dict[str, WorkspaceProfile] = {}  # New: workspace profiles
        self.registered_groups: dict[str, RegisteredGroup] = {}  # Legacy: backward compat
        self.last_agent_timestamp: dict[str, str] = {}
        self.message_loop_running: bool = False
        self.queue: GroupQueue = GroupQueue()
        self.channels: list[Channel] = []
        self._alias_to_canonical: dict[str, str] = {}
        self._canonical_to_aliases: dict[str, dict[str, str]] = {}
        self.event_bus: EventBus = EventBus()
        self._shutting_down: bool = False
        self._http_runner: Any | None = None
        self.plugin_manager: pluggy.PluginManager | None = None

        # Shared broadcast infrastructure — single code path for all channel sends.
        # Uses lambda so broadcaster always reads current self.channels reference.
        self._broadcaster = MessageBroadcaster(lambda: self.channels, self.get_channel_jid)
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

        # Load workspace profiles (new security-aware format)
        self.workspaces = await get_all_workspace_profiles()

        # Maintain backward compatibility with registered_groups
        self.registered_groups = {
            jid: profile.to_registered_group() for jid, profile in self.workspaces.items()
        }

        # Load JID alias cache
        await self._load_aliases()

        logger.info(
            "State loaded",
            workspace_count=len(self.workspaces),
            group_count=len(self.registered_groups),
            alias_count=len(self._alias_to_canonical),
        )

    async def _save_state(self) -> None:
        """Persist router state to the database."""
        await set_router_state("last_timestamp", self.last_timestamp)
        await set_router_state(
            "last_agent_timestamp",
            json.dumps(self.last_agent_timestamp),
        )

    # ------------------------------------------------------------------
    # JID alias cache
    # ------------------------------------------------------------------

    async def _load_aliases(self) -> None:
        """Populate the in-memory alias caches from the database."""
        all_aliases = await get_all_aliases()
        self._alias_to_canonical = dict(all_aliases)
        self._canonical_to_aliases = {}
        for cjid in set(all_aliases.values()):
            self._canonical_to_aliases[cjid] = await get_aliases_for_jid(cjid)

    def resolve_canonical_jid(self, jid: str) -> str:
        """Resolve an alias JID to its canonical JID. Returns jid itself if not an alias."""
        return self._alias_to_canonical.get(jid, jid)

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None:
        """Get the alias JID for a specific channel. Returns None if no alias exists."""
        aliases = self._canonical_to_aliases.get(canonical_jid, {})
        return aliases.get(channel_name)

    async def register_jid_alias(
        self, alias_jid: str, canonical_jid: str, channel_name: str
    ) -> None:
        """Persist a new alias and update the in-memory cache."""
        await set_jid_alias(alias_jid, canonical_jid, channel_name)
        self._alias_to_canonical[alias_jid] = canonical_jid
        self._canonical_to_aliases.setdefault(canonical_jid, {})[channel_name] = alias_jid
        logger.info(
            "JID alias registered",
            alias=alias_jid,
            canonical=canonical_jid,
            channel=channel_name,
        )

    # ------------------------------------------------------------------
    # Protocol adapter methods (satisfy handler Protocols via structural typing)
    # ------------------------------------------------------------------

    async def save_state(self) -> None:
        await self._save_state()

    async def handle_context_reset(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None:
        await session_handler.handle_context_reset(self, chat_jid, group, timestamp)

    async def handle_end_session(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None:
        await session_handler.handle_end_session(self, chat_jid, group, timestamp)

    async def trigger_manual_redeploy(self, chat_jid: str) -> None:
        await session_handler.trigger_manual_redeploy(self, chat_jid)

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict], *, source: str = "user"
    ) -> None:
        await output_handler.broadcast_agent_input(self, chat_jid, messages, source=source)

    async def run_agent(
        self,
        group: RegisteredGroup,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        project_access_override: bool | None = None,
        input_source: str = "user",
    ) -> str:
        from pynchy import agent_runner

        return await agent_runner.run_agent(
            self,
            group,
            chat_jid,
            messages,
            on_output,
            extra_system_notices,
            is_scheduled_task=is_scheduled_task,
            project_access_override=project_access_override,
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

    def _make_host_broadcaster(self) -> HostMessageBroadcaster:
        """Create a HostMessageBroadcaster wired to this app's store and event bus."""
        from pynchy.db import store_message_direct

        async def store_host_message(**kwargs: Any) -> None:
            await store_message_direct(**kwargs, message_type="host")

        async def store_system_notice(**kwargs: Any) -> None:
            await store_message_direct(**kwargs, message_type="user")

        return HostMessageBroadcaster(
            self._broadcaster, store_host_message, store_system_notice, self.event_bus.emit
        )

    async def handle_streamed_output(
        self, chat_jid: str, group: RegisteredGroup, result: ContainerOutput
    ) -> bool:
        return await output_handler.handle_streamed_output(self, chat_jid, group, result)

    # ------------------------------------------------------------------
    # Group management
    # ------------------------------------------------------------------

    async def _register_workspace(self, profile: WorkspaceProfile) -> None:
        """Register a new workspace and persist it."""
        self.workspaces[profile.jid] = profile
        self.registered_groups[profile.jid] = profile.to_registered_group()  # Backward compat
        await set_workspace_profile(profile)

        workspace_dir = get_settings().groups_dir / profile.folder
        (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

        logger.info(
            "Workspace registered",
            jid=profile.jid,
            name=profile.name,
            folder=profile.folder,
        )

    async def _register_group(self, jid: str, group: RegisteredGroup) -> None:
        """Register a new group and persist it.

        DEPRECATED: Use _register_workspace() instead.
        """
        profile = WorkspaceProfile.from_registered_group(jid, group)
        await self._register_workspace(profile)

        logger.info(
            "Group registered (legacy method)",
            jid=jid,
            name=group.name,
            folder=group.folder,
        )

    async def get_available_groups(self) -> list[dict[str, Any]]:
        """Get available groups list for the agent, ordered by most recent activity."""
        chats = await get_all_chats()
        registered_jids = set(self.registered_groups.keys())

        def is_channel_visible(jid: str) -> bool:
            if jid == "__group_sync__":
                return False

            # During startup/tests there may be no channels loaded yet; expose all
            # persisted chats so metadata APIs and snapshots remain available.
            if not self.channels:
                return True

            for ch in self.channels:
                try:
                    if ch.owns_jid(jid):
                        return True
                except Exception as exc:
                    logger.warning(
                        "Channel ownership check failed",
                        channel=ch.name,
                        jid=jid,
                        err=str(exc),
                    )
            return False

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

    async def _intercept_special_command(
        self, chat_jid: str, group: RegisteredGroup, message: NewMessage
    ) -> bool:
        """Delegates special-command handling to the message handler module."""
        return await message_handler.intercept_special_command(self, chat_jid, group, message)

    async def _process_group_messages(self, chat_jid: str) -> bool:
        """Delegates group processing to the message handler module."""
        return await message_handler.process_group_messages(self, chat_jid)

    async def _execute_direct_command(
        self, chat_jid: str, group: RegisteredGroup, message: NewMessage, command: str
    ) -> None:
        """Delegates direct command execution to the message handler module."""
        await message_handler.execute_direct_command(self, chat_jid, group, message, command)

    async def _broadcast_trace(
        self,
        chat_jid: str,
        trace_type: str,
        data: dict[str, Any],
        channel_text: str,
        *,
        db_id_prefix: str,
        db_sender: str,
        message_type: str = "assistant",
    ) -> None:
        """Delegates trace broadcasting to the output handler module."""
        await output_handler.broadcast_trace(
            self,
            chat_jid,
            trace_type,
            data,
            channel_text,
            db_id_prefix=db_id_prefix,
            db_sender=db_sender,
            message_type=message_type,
        )

    # ------------------------------------------------------------------
    # Message loop & startup delegation
    # ------------------------------------------------------------------

    async def _start_message_loop(self) -> None:
        """Main polling loop — delegated to message_handler."""
        if self.message_loop_running:
            logger.debug("Message loop already running, skipping duplicate start")
            return
        self.message_loop_running = True
        await message_handler.start_message_loop(self, lambda: self._shutting_down)

    async def _send_boot_notification(self) -> None:
        await startup_handler.send_boot_notification(self)

    async def _recover_pending_messages(self) -> None:
        await startup_handler.recover_pending_messages(self)

    async def _auto_rollback(self, continuation_path: Path, exc: Exception) -> None:
        await startup_handler.auto_rollback(continuation_path, exc)

    async def _check_deploy_continuation(self) -> None:
        await startup_handler.check_deploy_continuation(self)

    # Internal delegation for session_handler (used by dep_factory adapters)
    async def _ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None:
        await session_handler.ingest_user_message(self, msg, source_channel=source_channel)

    async def _on_inbound(self, _jid: str, msg: NewMessage) -> None:
        await session_handler.on_inbound(self, _jid, msg)

    async def _on_reaction(self, jid: str, message_ts: str, user_id: str, emoji: str) -> None:
        """Handle an inbound reaction from a channel."""
        from pynchy.messaging.reaction_handler import handle_reaction

        await handle_reaction(self, jid, message_ts, user_id, emoji)

    async def _send_clear_confirmation(self, chat_jid: str) -> None:
        await session_handler.send_clear_confirmation(self, chat_jid)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _pre_restart_cleanup(self) -> None:
        """Minimal cleanup before os.execv restart (plugin reload).

        Only the gateway and DB need closing — channels haven't connected
        yet and the queue has no in-flight work.
        """
        from pynchy.infra.gateway import stop_gateway

        await stop_gateway()

    async def _shutdown(self, sig_name: str) -> None:
        """Graceful shutdown handler. Second signal force-exits."""
        if self._shutting_down:
            logger.info("Force shutdown")
            os._exit(1)
        self._shutting_down = True
        logger.info("Shutdown signal received", signal=sig_name)

        # Hard-exit watchdog: if graceful shutdown hangs, force-exit after 12s.
        # This ensures launchd/systemd can restart us even if a container or
        # channel disconnect blocks indefinitely.
        loop = asyncio.get_running_loop()
        loop.call_later(12, lambda: os._exit(1))

        # Notify the god group that the service is going down.
        # Best-effort: don't let notification failure block shutdown.
        try:
            god_jid = next(
                (jid for jid, g in self.registered_groups.items() if g.is_god), None
            )
            if god_jid and self.channels:
                await self.broadcast_host_message(god_jid, f"Shutting down ({sig_name})")
        except Exception:
            logger.debug("Shutdown notification failed (ignored)")

        if self._http_runner:
            # Give SSE handlers a brief chance to observe shutdown state and
            # exit before aiohttp forcibly tears down request tasks.
            await asyncio.sleep(0.3)
            await self._http_runner.cleanup()

        from pynchy.infra.gateway import stop_gateway

        await stop_gateway()
        await self.queue.shutdown(10.0)
        for ch in self.channels:
            await ch.disconnect()

    async def run(self) -> None:
        """Main entry point — startup sequence."""
        from pynchy.dep_factory import (
            make_git_sync_deps,
            make_http_deps,
            make_ipc_deps,
            make_scheduler_deps,
        )
        from pynchy.git_ops.sync import start_host_git_sync_loop
        from pynchy.ipc import start_ipc_watcher
        from pynchy.task_scheduler import start_scheduler_loop

        s = get_settings()
        continuation_path = s.data_dir / "deploy_continuation.json"

        try:
            install_service()

            # Sync plugin repos, install only trusted / already-verified ones.
            # Unverified plugins are left out until audited below.
            synced = load_verified_plugins()

            from pynchy.plugin import get_plugin_manager
            from pynchy.workspace_config import configure_plugin_workspaces

            self.plugin_manager = get_plugin_manager()
            configure_plugin_workspaces(self.plugin_manager)
            ensure_container_system_running()

            # Start the LLM gateway before any containers launch so they can
            # reach it for credential-isolated API calls.
            from pynchy.infra.gateway import start_gateway

            await start_gateway()

            await init_database()
            logger.info("Database initialized")
            await self._load_state()
        except Exception as exc:
            # Auto-rollback if we crash during startup after a deploy
            if continuation_path.exists():
                await self._auto_rollback(continuation_path, exc)
            raise

        # Audit unverified plugins using the now-running container infra.
        # Blocks channel connections so no user messages arrive during audit.
        if await scan_and_install_new_plugins(synced, self.plugin_manager):
            await self._pre_restart_cleanup()
            import sys

            os.execv(sys.executable, [sys.executable] + sys.argv)

        loop = asyncio.get_running_loop()

        # Graceful shutdown
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.ensure_future(self._shutdown(s.name)),
            )

        context = ChannelPluginContext(
            on_message_callback=lambda jid, msg: asyncio.ensure_future(self._on_inbound(jid, msg)),
            on_chat_metadata_callback=lambda jid, ts, name=None: asyncio.ensure_future(
                store_chat_metadata(jid, ts, name)
            ),
            registered_groups=lambda: self.registered_groups,
            send_message=self.broadcast_to_channels,
            on_reaction_callback=lambda jid, ts, user, emoji: asyncio.ensure_future(
                self._on_reaction(jid, ts, user, emoji)
            ),
        )
        self.channels = load_channels(self.plugin_manager, context)
        default_channel = resolve_default_channel(self.channels)

        try:
            for ch in self.channels:
                await ch.connect()
        except Exception as exc:
            if continuation_path.exists():
                await self._auto_rollback(continuation_path, exc)
            raise

        # First-run: create a private group and register as god channel
        if not self.registered_groups:
            await startup_handler.setup_god_group(self, default_channel)

        # Reconcile worktrees: create missing ones for project_access groups,
        # fix broken worktrees, and rebase diverged branches before containers launch
        from pynchy.git_ops.worktree import reconcile_worktrees_at_startup
        from pynchy.workspace_config import get_project_access_folders, reconcile_workspaces

        project_access_folders = get_project_access_folders(self.workspaces)

        await asyncio.to_thread(
            reconcile_worktrees_at_startup,
            project_access_folders=project_access_folders,
        )

        # Reconcile workspaces (create chat groups + tasks from workspace.yaml)
        await reconcile_workspaces(
            registered_groups=self.registered_groups,
            channels=self.channels,
            register_fn=self._register_group,
            register_alias_fn=self.register_jid_alias,
            get_channel_jid_fn=self.get_channel_jid,
        )

        # Start subsystems
        asyncio.create_task(start_scheduler_loop(make_scheduler_deps(self)))
        asyncio.create_task(start_ipc_watcher(make_ipc_deps(self)))
        asyncio.create_task(start_host_git_sync_loop(make_git_sync_deps(self)))
        self.queue.set_process_messages_fn(self._process_group_messages)

        # HTTP server for remote health checks, deploys, and TUI API
        check_tunnels(self.plugin_manager)
        self._http_runner = await start_http_server(make_http_deps(self))
        logger.info("HTTP server ready", port=s.server.port)

        await self._send_boot_notification()
        await self._recover_pending_messages()
        await self._check_deploy_continuation()
        await self._start_message_loop()
