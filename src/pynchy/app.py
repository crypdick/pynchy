"""Main orchestrator — wires all subsystems together."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy import message_handler, output_handler, startup_handler
from pynchy.adapters import (
    EventBusAdapter,
    GroupMetadataManager,
    GroupRegistrationManager,
    GroupRegistry,
    HostMessageBroadcaster,
    MessageBroadcaster,
    PeriodicAgentManager,
    QueueManager,
    SessionManager,
    UserMessageHandler,
)
from pynchy.channel_runtime import ChannelPluginContext, load_channels, resolve_default_channel
from pynchy.config import get_settings
from pynchy.container_runner import (
    resolve_agent_core,
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.db import (
    clear_session,
    get_all_chats,
    get_all_sessions,
    get_all_tasks,
    get_all_workspace_profiles,
    get_router_state,
    init_database,
    set_chat_cleared_at,
    set_router_state,
    set_session,
    set_workspace_profile,
    store_chat_metadata,
    store_message,
    store_message_direct,
)
from pynchy.event_bus import (
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)
from pynchy.git_sync import start_host_git_sync_loop
from pynchy.git_utils import count_unpushed_commits, get_head_sha, is_repo_dirty
from pynchy.group_queue import GroupQueue
from pynchy.http_server import (
    start_http_server,
)
from pynchy.ipc import start_ipc_watcher
from pynchy.logger import logger
from pynchy.service_installer import install_service
from pynchy.system_checks import check_tailscale, ensure_container_system_running
from pynchy.task_scheduler import start_scheduler_loop
from pynchy.types import (
    Channel,
    ContainerInput,
    ContainerOutput,
    NewMessage,
    RegisteredGroup,
    WorkspaceProfile,
)
from pynchy.utils import generate_message_id


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
        self.event_bus: EventBus = EventBus()
        self._shutting_down: bool = False
        self._http_runner: Any | None = None
        self.plugin_manager: pluggy.PluginManager | None = None

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

        logger.info(
            "State loaded",
            workspace_count=len(self.workspaces),
            group_count=len(self.registered_groups),
        )

    async def _save_state(self) -> None:
        """Persist router state to the database."""
        await set_router_state("last_timestamp", self.last_timestamp)
        await set_router_state(
            "last_agent_timestamp",
            json.dumps(self.last_agent_timestamp),
        )

    # Adapter methods for extracted handler protocols
    async def save_state(self) -> None:
        await self._save_state()

    async def handle_context_reset(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None:
        await self._handle_context_reset(chat_jid, group, timestamp)

    async def handle_end_session(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None:
        await self._handle_end_session(chat_jid, group, timestamp)

    async def trigger_manual_redeploy(self, chat_jid: str) -> None:
        await self._trigger_manual_redeploy(chat_jid)

    async def run_agent(
        self,
        group: RegisteredGroup,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None = None,
        extra_system_notices: list[str] | None = None,
    ) -> str:
        return await self._run_agent(group, chat_jid, messages, on_output, extra_system_notices)

    def emit(self, event: Any) -> None:
        self.event_bus.emit(event)

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

        return [
            {
                "jid": c["jid"],
                "name": c["name"],
                "lastActivity": c["last_message_time"],
                "isRegistered": c["jid"] in registered_jids,
            }
            for c in chats
            if c["jid"] != "__group_sync__" and c["jid"].endswith("@g.us")
        ]

    # ------------------------------------------------------------------
    # First-run setup
    # ------------------------------------------------------------------

    async def _setup_god_group(self, default_channel: Any) -> None:
        """Create a new group and register it as the god channel.

        Called on first run when no groups are registered. Creates a private
        group so the user has a dedicated space to talk to the agent.
        """
        s = get_settings()
        group_name = s.agent.name.title()
        logger.info("No groups registered. Creating default channel group...", name=group_name)

        jid = await default_channel.create_group(group_name)

        # Create god workspace with default security profile
        profile = WorkspaceProfile(
            jid=jid,
            name=group_name,
            folder=s.agent.name,
            trigger=f"@{s.agent.name}",
            added_at=datetime.now(UTC).isoformat(),
            requires_trigger=False,
            is_god=True,
        )
        await self._register_workspace(profile)
        logger.info("God channel created", group=group_name, jid=jid)

    def _validate_plugin_credentials(self, plugin: Any) -> list[str]:
        """Check if plugin has required environment variables.

        Args:
            plugin: Plugin instance with optional requires_credentials() method

        Returns:
            List of missing credential names (empty if all present)
        """
        if not hasattr(plugin, "requires_credentials"):
            return []

        required = plugin.requires_credentials()
        missing = [cred for cred in required if cred not in os.environ]
        return missing

    # ------------------------------------------------------------------
    # Message processing
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

    async def _handle_streamed_output(
        self, chat_jid: str, group: RegisteredGroup, result: ContainerOutput
    ) -> bool:
        """Delegates streamed output handling to the output handler module."""
        return await output_handler.handle_streamed_output(self, chat_jid, group, result)

    async def handle_streamed_output(
        self, chat_jid: str, group: RegisteredGroup, result: ContainerOutput
    ) -> bool:
        return await self._handle_streamed_output(chat_jid, group, result)

    async def _run_agent(
        self,
        group: RegisteredGroup,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None = None,
        extra_system_notices: list[str] | None = None,
    ) -> str:
        """Run the container agent for a group. Returns 'success' or 'error'."""
        from pynchy.workspace_config import has_project_access

        is_god = group.is_god
        project_access = has_project_access(group)
        session_id = self.sessions.get(group.folder)

        # Update snapshots for container to read
        tasks = await get_all_tasks()
        write_tasks_snapshot(
            group.folder,
            is_god,
            [t.to_snapshot_dict() for t in tasks],
        )

        available_groups = await self.get_available_groups()
        write_groups_snapshot(
            group.folder,
            is_god,
            available_groups,
            set(self.registered_groups.keys()),
        )

        # Wrap on_output to track session ID from streamed results
        async def wrapped_on_output(output: ContainerOutput) -> None:
            if output.new_session_id and group.folder not in self._session_cleared:
                self.sessions[group.folder] = output.new_session_id
                await set_session(group.folder, output.new_session_id)
            if on_output:
                await on_output(output)

        # Build system notices for the LLM (SDK system messages, NOT host messages)
        # These are sent TO the LLM as context, distinct from operational host messages
        system_notices: list[str] = []
        if is_god:
            if is_repo_dirty():
                system_notices.append(
                    "There are uncommitted local changes. Run `git status` and `git diff` "
                    "to review them. If they are good, commit and push. If not, discard them."
                )
            if count_unpushed_commits() > 0:
                system_notices.append(
                    "There are local commits that haven't been pushed. "
                    "Run `git push` or `git rebase origin/main && git push` to sync them."
                )
            if system_notices:
                system_notices.append(
                    "Consider whether to address these issues "
                    "before or after handling the new message."
                )

        # Add any extra system notices passed in
        if extra_system_notices:
            if system_notices:
                system_notices.extend(extra_system_notices)
            else:
                system_notices = extra_system_notices[:]

        # Clear the guard — this container run starts fresh
        self._session_cleared.discard(group.folder)

        # system_notices are handled via system_prompt in the container (ephemeral context)
        # messages contains the persistent conversation history (with message types)
        # The container appends system_notices to the SDK system_prompt parameter

        agent_core_module, agent_core_class = resolve_agent_core(self.plugin_manager)

        try:
            output = await run_container_agent(
                group=group,
                input_data=ContainerInput(
                    messages=messages,
                    session_id=session_id,
                    group_folder=group.folder,
                    chat_jid=chat_jid,
                    is_god=is_god,
                    system_notices=system_notices or None,
                    project_access=project_access,
                    agent_core_module=agent_core_module,
                    agent_core_class=agent_core_class,
                ),
                on_process=lambda proc, name: self.queue.register_process(
                    chat_jid, proc, name, group.folder
                ),
                on_output=wrapped_on_output if on_output else None,
                plugin_manager=self.plugin_manager,
            )

            if output.new_session_id and group.folder not in self._session_cleared:
                self.sessions[group.folder] = output.new_session_id
                await set_session(group.folder, output.new_session_id)

            if output.status == "error":
                logger.error(
                    "Container agent error",
                    group=group.name,
                    error=output.error,
                )
                return "error"

            return "success"
        except Exception as exc:
            logger.error("Agent error", group=group.name, err=str(exc))
            return "error"

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _start_message_loop(self) -> None:
        """Main polling loop — delegated to message_handler."""
        if self.message_loop_running:
            logger.debug("Message loop already running, skipping duplicate start")
            return
        self.message_loop_running = True
        await message_handler.start_message_loop(self, lambda: self._shutting_down)

    async def _send_boot_notification(self) -> None:
        """Delegates startup boot message handling."""
        await startup_handler.send_boot_notification(self)

    async def _recover_pending_messages(self) -> None:
        """Delegates startup message recovery."""
        await startup_handler.recover_pending_messages(self)

    # ------------------------------------------------------------------
    # Deploy rollback
    # ------------------------------------------------------------------

    async def _auto_rollback(self, continuation_path: Path, exc: Exception) -> None:
        """Delegates rollback logic."""
        await startup_handler.auto_rollback(continuation_path, exc)

    # ------------------------------------------------------------------
    # Deploy continuation
    # ------------------------------------------------------------------

    async def _check_deploy_continuation(self) -> None:
        """Delegates deploy continuation handling."""
        await startup_handler.check_deploy_continuation(self)

    # ------------------------------------------------------------------
    # Channel broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
    ) -> None:
        """Send a message to all connected channels.

        Args:
            chat_jid: Target chat JID
            text: Message text to send
            suppress_errors: If True, silently ignore channel send failures
        """
        for ch in self.channels:
            if ch.is_connected():
                if suppress_errors:
                    try:
                        await ch.send_message(chat_jid, text)
                    except (OSError, TimeoutError, ConnectionError) as exc:
                        logger.warning("Channel send failed", channel=ch.name, err=str(exc))
                else:
                    try:
                        await ch.send_message(chat_jid, text)
                    except Exception as exc:
                        logger.warning("Channel send failed", channel=ch.name, err=str(exc))

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
    ) -> None:
        await self._broadcast_to_channels(chat_jid, text, suppress_errors=suppress_errors)

    async def _send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None:
        """Send a reaction emoji to a message on all channels that support it."""
        for ch in self.channels:
            if ch.is_connected() and hasattr(ch, "send_reaction"):
                try:
                    await ch.send_reaction(chat_jid, message_id, sender, emoji)
                except (OSError, TimeoutError, ConnectionError) as exc:
                    logger.debug("Reaction send failed", channel=ch.name, err=str(exc))

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None:
        await self._send_reaction_to_channels(chat_jid, message_id, sender, emoji)

    async def _set_typing_on_channels(self, chat_jid: str, is_typing: bool) -> None:
        """Set typing indicator on all channels that support it."""
        for ch in self.channels:
            if ch.is_connected() and hasattr(ch, "set_typing"):
                try:
                    await ch.set_typing(chat_jid, is_typing)
                except (OSError, TimeoutError, ConnectionError) as exc:
                    logger.debug("Typing indicator send failed", channel=ch.name, err=str(exc))

    async def set_typing_on_channels(self, chat_jid: str, is_typing: bool) -> None:
        await self._set_typing_on_channels(chat_jid, is_typing)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _broadcast_host_message(self, chat_jid: str, text: str) -> None:
        """Send operational notification from the host/platform to the user.

        Host messages are purely operational notifications (errors, status updates,
        confirmations) that are OUTSIDE the LLM's conversation. They are:
        - Sent to the user via channels (WhatsApp, etc.)
        - Stored in message history for user reference
        - NOT sent to the LLM as system messages or user messages
        - NOT part of the SDK conversation flow

        This is distinct from SDK system messages, which provide context TO the LLM.

        Examples: "⚠️ Agent error occurred", "Context cleared", deployment notifications.
        """
        ts = datetime.now(UTC).isoformat()
        await store_message_direct(
            id=generate_message_id("host"),
            chat_jid=chat_jid,
            sender="host",
            sender_name="host",
            content=text,
            timestamp=ts,
            is_from_me=True,
            message_type="host",
        )
        channel_text = f"\U0001f3e0 {text}"
        await self._broadcast_to_channels(chat_jid, channel_text)
        self.event_bus.emit(
            MessageEvent(
                chat_jid=chat_jid,
                sender_name="host",
                content=text,
                timestamp=ts,
                is_bot=True,
            )
        )

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        await self._broadcast_host_message(chat_jid, text)

    async def _handle_context_reset(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None:
        """Clear session state, merge worktree, and confirm context reset."""
        from pynchy.workspace_config import has_project_access
        from pynchy.worktree import merge_and_push_worktree

        # Merge worktree commits before clearing session so work isn't stranded
        if has_project_access(group):
            asyncio.create_task(asyncio.to_thread(merge_and_push_worktree, group.folder))

        self.sessions.pop(group.folder, None)
        self._session_cleared.add(group.folder)
        await clear_session(group.folder)
        self.queue.close_stdin(chat_jid)
        self.last_agent_timestamp[chat_jid] = timestamp
        await self._save_state()
        await self._send_clear_confirmation(chat_jid)

    async def _handle_end_session(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None:
        """Sync worktree and spin down the container without clearing context.

        Unlike context reset, this preserves conversation history. The next
        message will start a fresh container that picks up where it left off.
        """
        from pynchy.workspace_config import has_project_access
        from pynchy.worktree import merge_and_push_worktree

        # Merge worktree commits before stopping so work isn't stranded
        if has_project_access(group):
            asyncio.create_task(asyncio.to_thread(merge_and_push_worktree, group.folder))

        # Stop the container but keep session state intact
        self.queue.close_stdin(chat_jid)
        self.last_agent_timestamp[chat_jid] = timestamp
        await self._save_state()
        await self._broadcast_host_message(chat_jid, "👋")

    async def _send_clear_confirmation(self, chat_jid: str) -> None:
        """Set cleared_at, store and broadcast a system confirmation."""
        # Mark clear boundary — messages before this are hidden
        cleared_ts = datetime.now(UTC).isoformat()
        await set_chat_cleared_at(chat_jid, cleared_ts)
        self.event_bus.emit(ChatClearedEvent(chat_jid=chat_jid))

        await self._broadcast_host_message(chat_jid, "🗑️")

    async def _trigger_manual_redeploy(self, chat_jid: str) -> None:
        """Handle a manual redeploy command — restart the service in-place."""
        from pynchy.deploy import finalize_deploy

        sha = get_head_sha()
        logger.info("Manual redeploy triggered via magic word", chat_jid=chat_jid)
        await finalize_deploy(
            broadcast_host_message=self._broadcast_host_message,
            chat_jid=chat_jid,
            commit_sha=sha,
            previous_sha=sha,
        )

    async def _ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None:
        """Unified user message ingestion — stores, emits, and broadcasts to all channels.

        This is the common code path for ALL user inputs from ANY UI:
        - WhatsApp messages
        - TUI messages
        - Telegram messages
        - Any future channels

        Args:
            msg: The user message to ingest
            source_channel: Optional name of the originating channel (e.g., "whatsapp", "tui").
                           If provided, we skip broadcasting back to that channel.
        """
        # 1. Store in database
        await store_message(msg)

        # 2. Emit to event bus (for TUI/SSE, logging, etc.)
        self.event_bus.emit(
            MessageEvent(
                chat_jid=msg.chat_jid,
                sender_name=msg.sender_name,
                content=msg.content,
                timestamp=msg.timestamp,
                is_bot=False,
            )
        )

        # 3. Broadcast to all connected channels (except source)
        # This ensures messages from one UI appear in all other UIs
        for ch in self.channels:
            if ch.is_connected():
                # Skip broadcasting back to the source channel
                if source_channel and ch.name == source_channel:
                    continue

                # Format the message with sender name
                formatted = f"{msg.sender_name}: {msg.content}"
                try:
                    await ch.send_message(msg.chat_jid, formatted)
                except (OSError, TimeoutError, ConnectionError) as exc:
                    logger.warning("Cross-channel broadcast failed", channel=ch.name, err=str(exc))

    async def _on_inbound(self, _jid: str, msg: NewMessage) -> None:
        """Handle inbound message from any channel — delegates to unified ingestion."""
        # Find which channel this came from
        source_channel = None
        for ch in self.channels:
            if ch.owns_jid(msg.chat_jid):
                source_channel = ch.name
                break

        await self._ingest_user_message(msg, source_channel=source_channel)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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

        if self._http_runner:
            # Give SSE handlers a brief chance to observe shutdown state and
            # exit before aiohttp forcibly tears down request tasks.
            await asyncio.sleep(0.3)
            await self._http_runner.cleanup()
        await self.queue.shutdown(10.0)
        for channel in self.channels:
            await channel.disconnect()

    async def run(self) -> None:
        """Main entry point — startup sequence."""
        s = get_settings()
        continuation_path = s.data_dir / "deploy_continuation.json"

        try:
            install_service()
            ensure_container_system_running()
            await init_database()
            logger.info("Database initialized")
            await self._load_state()

            # Initialize plugin manager after loading state
            from pynchy.plugin import get_plugin_manager

            self.plugin_manager = get_plugin_manager()
        except Exception as exc:
            # Auto-rollback if we crash during startup after a deploy
            if continuation_path.exists():
                await self._auto_rollback(continuation_path, exc)
            raise

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
            send_message=self._broadcast_to_channels,
        )
        self.channels = load_channels(self.plugin_manager, context)
        default_channel = resolve_default_channel(self.channels)

        try:
            for channel in self.channels:
                await channel.connect()
        except Exception as exc:
            if continuation_path.exists():
                await self._auto_rollback(continuation_path, exc)
            raise

        # First-run: create a private group and register as god channel
        if not self.registered_groups:
            await self._setup_god_group(default_channel)

        # Reconcile worktrees: create missing ones for project_access groups,
        # fix broken worktrees, and rebase diverged branches before containers launch
        from pynchy.workspace_config import get_project_access_folders, reconcile_workspaces
        from pynchy.worktree import reconcile_worktrees_at_startup

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
        )

        # Start subsystems
        asyncio.create_task(start_scheduler_loop(self._make_scheduler_deps()))
        asyncio.create_task(start_ipc_watcher(self._make_ipc_deps()))
        asyncio.create_task(start_host_git_sync_loop(self._make_git_sync_deps()))
        self.queue.set_process_messages_fn(self._process_group_messages)

        # HTTP server for remote health checks, deploys, and TUI API
        check_tailscale()
        self._http_runner = await start_http_server(self._make_http_deps())
        logger.info("HTTP server ready", port=s.server.port)

        await self._send_boot_notification()
        await self._recover_pending_messages()
        await self._check_deploy_continuation()
        await self._start_message_loop()

    # ------------------------------------------------------------------
    # Dependency adapters
    # ------------------------------------------------------------------

    def _make_host_broadcaster(self) -> tuple[MessageBroadcaster, HostMessageBroadcaster]:
        """Create a MessageBroadcaster and HostMessageBroadcaster pair.

        Shared factory for dependency adapters that need to send host messages.
        The store function injects message_type='host' so host messages are
        filtered out of the LLM conversation context.
        """
        broadcaster = MessageBroadcaster(self.channels)

        async def store_host_message(**kwargs: Any) -> None:
            await store_message_direct(**kwargs, message_type="host")

        host_broadcaster = HostMessageBroadcaster(
            broadcaster, store_host_message, self.event_bus.emit
        )
        return broadcaster, host_broadcaster

    def _make_scheduler_deps(self) -> Any:
        """Create the dependency object for the task scheduler."""
        # Use composition of adapters instead of manual delegation
        group_registry = GroupRegistry(self.registered_groups)
        session_manager = SessionManager(self.sessions, self._session_cleared)
        queue_manager = QueueManager(self.queue)
        broadcaster = MessageBroadcaster(self.channels)

        # Return a composite object that provides all required deps
        class SchedulerDeps:
            registered_groups = group_registry.registered_groups
            get_sessions = session_manager.get_sessions
            queue = queue_manager.queue
            on_process = queue_manager.on_process
            broadcast_to_channels = broadcaster._broadcast_formatted
            plugin_manager = self.plugin_manager

        return SchedulerDeps()

    def _make_http_deps(self) -> Any:
        """Create the dependency object for the HTTP server."""
        app = self
        _broadcaster, host_broadcaster = self._make_host_broadcaster()
        group_registry = GroupRegistry(self.registered_groups)
        metadata_manager = GroupMetadataManager(
            self.registered_groups, self.channels, self.get_available_groups
        )
        periodic_agent_manager = PeriodicAgentManager(self.registered_groups)
        user_message_handler = UserMessageHandler(
            self._ingest_user_message, self.queue.enqueue_message_check
        )
        event_adapter = EventBusAdapter(self.event_bus)

        # Return a composite object that provides all required deps
        class HttpDeps:
            broadcast_host_message = host_broadcaster.broadcast_host_message
            god_chat_jid = group_registry.god_chat_jid
            channels_connected = metadata_manager.channels_connected
            get_groups = metadata_manager.get_groups
            get_messages = user_message_handler.get_messages
            send_user_message = user_message_handler.send_user_message
            get_periodic_agents = periodic_agent_manager.get_periodic_agents
            subscribe_events = event_adapter.subscribe_events

            def is_shutting_down(self) -> bool:
                return app._shutting_down

        return HttpDeps()

    def _make_ipc_deps(self) -> Any:
        """Create the dependency object for the IPC watcher."""
        broadcaster, host_broadcaster = self._make_host_broadcaster()
        registration_manager = GroupRegistrationManager(
            self.registered_groups, self._register_group, self._send_clear_confirmation
        )
        session_manager = SessionManager(self.sessions, self._session_cleared)
        metadata_manager = GroupMetadataManager(
            self.registered_groups, self.channels, self.get_available_groups
        )
        queue_manager = QueueManager(self.queue)

        # Return a composite object that provides all required deps
        class IpcDeps:
            broadcast_to_channels = broadcaster._broadcast_to_channels
            broadcast_host_message = host_broadcaster.broadcast_host_message
            broadcast_system_notice = host_broadcaster.broadcast_system_notice
            registered_groups = registration_manager.registered_groups
            register_group = registration_manager.register_group
            sync_group_metadata = metadata_manager.sync_group_metadata
            get_available_groups = metadata_manager.get_available_groups
            write_groups_snapshot = staticmethod(write_groups_snapshot)
            clear_session = session_manager.clear_session
            clear_chat_history = registration_manager.clear_chat_history
            enqueue_message_check = queue_manager.enqueue_message_check
            channels = metadata_manager.channels

        return IpcDeps()

    def _make_git_sync_deps(self) -> Any:
        """Create the dependency object for the git sync loop."""
        _broadcaster, host_broadcaster = self._make_host_broadcaster()
        group_registry = GroupRegistry(self.registered_groups)

        class GitSyncDeps:
            broadcast_system_notice = host_broadcaster.broadcast_system_notice

            def registered_groups(self) -> dict[str, Any]:
                return group_registry.registered_groups()

            async def trigger_deploy(self, previous_sha: str) -> None:
                s = get_settings()
                chat_jid = group_registry.god_chat_jid()
                if chat_jid:
                    await host_broadcaster.broadcast_host_message(
                        chat_jid,
                        "Container files changed on origin — rebuilding and restarting...",
                    )

                # Rebuild container image
                build_script = s.project_root / "container" / "build.sh"
                if build_script.exists():
                    result = subprocess.run(
                        [str(build_script)],
                        cwd=str(s.project_root / "container"),
                        capture_output=True,
                        text=True,
                        timeout=600,
                    )
                    if result.returncode != 0:
                        logger.error(
                            "Container rebuild failed during sync",
                            stderr=result.stderr[-500:],
                        )

                from pynchy.deploy import finalize_deploy

                await finalize_deploy(
                    broadcast_host_message=host_broadcaster.broadcast_host_message,
                    chat_jid=chat_jid,
                    commit_sha=get_head_sha(),
                    previous_sha=previous_sha,
                )

        return GitSyncDeps()
