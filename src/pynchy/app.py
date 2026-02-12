"""Main orchestrator — wires all subsystems together.

Port of src/index.ts. Module-level globals become instance state on PynchyApp.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import subprocess
from typing import Any

from pynchy.config import (
    ASSISTANT_NAME,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    POLL_INTERVAL,
    TRIGGER_PATTERN,
)
from pynchy.container_runner import (
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.db import (
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    init_database,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
)
from pynchy.group_queue import GroupQueue
from pynchy.ipc import start_ipc_watcher
from pynchy.logger import logger
from pynchy.router import format_messages, format_outbound
from pynchy.task_scheduler import start_scheduler_loop
from pynchy.types import Channel, ContainerInput, ContainerOutput, NewMessage, RegisteredGroup


class PynchyApp:
    """Main application class — owns all runtime state and wires subsystems."""

    def __init__(self) -> None:
        self.last_timestamp: str = ""
        self.sessions: dict[str, str] = {}
        self.registered_groups: dict[str, RegisteredGroup] = {}
        self.last_agent_timestamp: dict[str, str] = {}
        self.message_loop_running: bool = False
        self.queue: GroupQueue = GroupQueue()
        self.channels: list[Channel] = []

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
        self.registered_groups = await get_all_registered_groups()
        logger.info(
            "State loaded",
            group_count=len(self.registered_groups),
        )

    async def _save_state(self) -> None:
        """Persist router state to the database."""
        await set_router_state("last_timestamp", self.last_timestamp)
        await set_router_state(
            "last_agent_timestamp",
            json.dumps(self.last_agent_timestamp),
        )

    # ------------------------------------------------------------------
    # Group management
    # ------------------------------------------------------------------

    async def _register_group(self, jid: str, group: RegisteredGroup) -> None:
        """Register a new group and persist it."""
        self.registered_groups[jid] = group
        await set_registered_group(jid, group)

        group_dir = GROUPS_DIR / group.folder
        (group_dir / "logs").mkdir(parents=True, exist_ok=True)

        logger.info(
            "Group registered",
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
    # Message processing
    # ------------------------------------------------------------------

    async def _process_group_messages(self, chat_jid: str) -> bool:
        """Process all pending messages for a group. Called by GroupQueue."""
        group = self.registered_groups.get(chat_jid)
        if not group:
            return True

        is_main_group = group.folder == MAIN_GROUP_FOLDER
        since_timestamp = self.last_agent_timestamp.get(chat_jid, "")
        missed_messages = await get_messages_since(chat_jid, since_timestamp, ASSISTANT_NAME)

        if not missed_messages:
            return True

        # For non-main groups, check if trigger is required and present
        if not is_main_group and group.requires_trigger is not False:
            has_trigger = any(TRIGGER_PATTERN.search(m.content.strip()) for m in missed_messages)
            if not has_trigger:
                return True

        prompt = format_messages(missed_messages)

        # Advance cursor; save old cursor for rollback on error
        previous_cursor = self.last_agent_timestamp.get(chat_jid, "")
        self.last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
        await self._save_state()

        logger.info(
            "Processing messages",
            group=group.name,
            message_count=len(missed_messages),
        )

        # Track idle timer for closing stdin when agent is idle
        loop = asyncio.get_running_loop()
        idle_handle: asyncio.TimerHandle | None = None

        def reset_idle_timer() -> None:
            nonlocal idle_handle
            if idle_handle is not None:
                idle_handle.cancel()
            idle_handle = loop.call_later(
                IDLE_TIMEOUT,
                lambda: self.queue.close_stdin(chat_jid),
            )

        # Set typing indicator on the appropriate channel
        channel = self._find_channel(chat_jid)
        if channel and hasattr(channel, "set_typing"):
            await channel.set_typing(chat_jid, True)

        had_error = False
        output_sent_to_user = False

        async def on_output(result: ContainerOutput) -> None:
            nonlocal had_error, output_sent_to_user
            if result.result:
                raw = result.result
                from pynchy.router import strip_internal_tags

                text = strip_internal_tags(raw)
                logger.info("Agent output", group=group.name, text=raw[:200])
                if text and channel:
                    await channel.send_message(chat_jid, f"{ASSISTANT_NAME}: {text}")
                    output_sent_to_user = True
                # Only reset idle timer on actual results, not session-update markers
                reset_idle_timer()

            if result.status == "error":
                had_error = True

        agent_result = await self._run_agent(group, prompt, chat_jid, on_output)

        if channel and hasattr(channel, "set_typing"):
            await channel.set_typing(chat_jid, False)
        if idle_handle is not None:
            idle_handle.cancel()

        if agent_result == "error" or had_error:
            if output_sent_to_user:
                logger.warning(
                    "Agent error after output was sent, skipping cursor rollback",
                    group=group.name,
                )
                return True
            # Roll back cursor for retry
            self.last_agent_timestamp[chat_jid] = previous_cursor
            await self._save_state()
            logger.warning(
                "Agent error, rolled back message cursor for retry",
                group=group.name,
            )
            return False

        return True

    async def _run_agent(
        self,
        group: RegisteredGroup,
        prompt: str,
        chat_jid: str,
        on_output: Any | None = None,
    ) -> str:
        """Run the container agent for a group. Returns 'success' or 'error'."""
        is_main = group.folder == MAIN_GROUP_FOLDER
        session_id = self.sessions.get(group.folder)

        # Update snapshots for container to read
        tasks = await get_all_tasks()
        write_tasks_snapshot(
            group.folder,
            is_main,
            [
                {
                    "id": t.id,
                    "groupFolder": t.group_folder,
                    "prompt": t.prompt,
                    "schedule_type": t.schedule_type,
                    "schedule_value": t.schedule_value,
                    "status": t.status,
                    "next_run": t.next_run,
                }
                for t in tasks
            ],
        )

        available_groups = await self.get_available_groups()
        write_groups_snapshot(
            group.folder,
            is_main,
            available_groups,
            set(self.registered_groups.keys()),
        )

        # Wrap on_output to track session ID from streamed results
        async def wrapped_on_output(output: ContainerOutput) -> None:
            if output.new_session_id:
                self.sessions[group.folder] = output.new_session_id
                await set_session(group.folder, output.new_session_id)
            if on_output:
                await on_output(output)

        try:
            output = await run_container_agent(
                group=group,
                input_data=ContainerInput(
                    prompt=prompt,
                    session_id=session_id,
                    group_folder=group.folder,
                    chat_jid=chat_jid,
                    is_main=is_main,
                ),
                on_process=lambda proc, name: self.queue.register_process(
                    chat_jid, proc, name, group.folder
                ),
                on_output=wrapped_on_output if on_output else None,
            )

            if output.new_session_id:
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
        """Main polling loop — checks for new messages every POLL_INTERVAL."""
        if self.message_loop_running:
            logger.debug("Message loop already running, skipping duplicate start")
            return
        self.message_loop_running = True

        logger.info(f"Pynchy running (trigger: @{ASSISTANT_NAME})")

        while True:
            try:
                jids = list(self.registered_groups.keys())
                messages, new_timestamp = await get_new_messages(
                    jids, self.last_timestamp, ASSISTANT_NAME
                )

                if messages:
                    logger.info("New messages", count=len(messages))

                    # Advance "seen" cursor immediately
                    self.last_timestamp = new_timestamp
                    await self._save_state()

                    # Group by chat JID
                    messages_by_group: dict[str, list[NewMessage]] = {}
                    for msg in messages:
                        messages_by_group.setdefault(msg.chat_jid, []).append(msg)

                    for chat_jid, group_messages in messages_by_group.items():
                        group = self.registered_groups.get(chat_jid)
                        if not group:
                            continue

                        is_main_group = group.folder == MAIN_GROUP_FOLDER
                        needs_trigger = not is_main_group and group.requires_trigger is not False

                        if needs_trigger:
                            has_trigger = any(
                                TRIGGER_PATTERN.search(m.content.strip())
                                for m in group_messages
                            )
                            if not has_trigger:
                                continue

                        # Pull all messages since lastAgentTimestamp for context
                        all_pending = await get_messages_since(
                            chat_jid,
                            self.last_agent_timestamp.get(chat_jid, ""),
                            ASSISTANT_NAME,
                        )
                        messages_to_send = all_pending if all_pending else group_messages
                        formatted = format_messages(messages_to_send)

                        if self.queue.send_message(chat_jid, formatted):
                            logger.debug(
                                "Piped messages to active container",
                                chat_jid=chat_jid,
                                count=len(messages_to_send),
                            )
                            self.last_agent_timestamp[chat_jid] = messages_to_send[
                                -1
                            ].timestamp
                            await self._save_state()
                        else:
                            self.queue.enqueue_message_check(chat_jid)

            except Exception as exc:
                logger.error("Error in message loop", err=str(exc))

            await asyncio.sleep(POLL_INTERVAL)

    async def _recover_pending_messages(self) -> None:
        """Startup recovery: check for unprocessed messages in registered groups."""
        for chat_jid, group in self.registered_groups.items():
            since_timestamp = self.last_agent_timestamp.get(chat_jid, "")
            pending = await get_messages_since(chat_jid, since_timestamp, ASSISTANT_NAME)
            if pending:
                logger.info(
                    "Recovery: found unprocessed messages",
                    group=group.name,
                    pending_count=len(pending),
                )
                self.queue.enqueue_message_check(chat_jid)

    # ------------------------------------------------------------------
    # Container system
    # ------------------------------------------------------------------

    def _ensure_container_system_running(self) -> None:
        """Check Apple Container is running, start if needed, kill orphans."""
        try:
            subprocess.run(
                ["container", "system", "status"],
                capture_output=True,
                check=True,
            )
            logger.debug("Apple Container system already running")
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.info("Starting Apple Container system...")
            try:
                subprocess.run(
                    ["container", "system", "start"],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                logger.info("Apple Container system started")
            except Exception as exc:
                logger.error("Failed to start Apple Container system", err=str(exc))
                raise RuntimeError(
                    "Apple Container system is required but failed to start"
                ) from exc

        # Kill orphaned containers from previous runs
        try:
            result = subprocess.run(
                ["container", "ls", "--format", "json"],
                capture_output=True,
                text=True,
            )
            containers = json.loads(result.stdout or "[]")
            orphans = [
                c["configuration"]["id"]
                for c in containers
                if c.get("status") == "running"
                and c.get("configuration", {}).get("id", "").startswith("pynchy-")
            ]
            for name in orphans:
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["container", "stop", name],
                        capture_output=True,
                    )
            if orphans:
                logger.info(
                    "Stopped orphaned containers",
                    count=len(orphans),
                    names=orphans,
                )
        except Exception as exc:
            logger.warning("Failed to clean up orphaned containers", err=str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_channel(self, jid: str) -> Channel | None:
        """Find the channel that owns a given JID."""
        for c in self.channels:
            if c.owns_jid(jid):
                return c
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _shutdown(self, sig_name: str) -> None:
        """Graceful shutdown handler."""
        logger.info("Shutdown signal received", signal=sig_name)
        await self.queue.shutdown(10.0)
        for channel in self.channels:
            await channel.disconnect()

    async def run(self) -> None:
        """Main entry point — startup sequence."""
        self._ensure_container_system_running()
        await init_database()
        logger.info("Database initialized")
        await self._load_state()

        loop = asyncio.get_running_loop()

        # Graceful shutdown
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.ensure_future(self._shutdown(s.name)),
            )

        # Create and connect WhatsApp channel
        from pynchy.whatsapp import WhatsAppChannel

        whatsapp = WhatsAppChannel(
            on_message=lambda _jid, msg: asyncio.ensure_future(store_message(msg)),
            on_chat_metadata=lambda jid, ts: asyncio.ensure_future(
                store_chat_metadata(jid, ts)
            ),
            registered_groups=lambda: self.registered_groups,
        )
        self.channels.append(whatsapp)
        await whatsapp.connect()

        # Start subsystems
        asyncio.create_task(
            start_scheduler_loop(self._make_scheduler_deps())
        )
        asyncio.create_task(
            start_ipc_watcher(self._make_ipc_deps())
        )
        self.queue.set_process_messages_fn(self._process_group_messages)
        await self._recover_pending_messages()
        await self._start_message_loop()

    # ------------------------------------------------------------------
    # Dependency adapters
    # ------------------------------------------------------------------

    def _make_scheduler_deps(self) -> Any:
        """Create the dependency object for the task scheduler."""
        app = self

        class _Deps:
            def registered_groups(self) -> dict[str, RegisteredGroup]:
                return app.registered_groups

            def get_sessions(self) -> dict[str, str]:
                return app.sessions

            @property
            def queue(self) -> GroupQueue:
                return app.queue

            def on_process(
                self, group_jid: str, proc: Any, container_name: str, group_folder: str
            ) -> None:
                app.queue.register_process(group_jid, proc, container_name, group_folder)

            async def send_message(self, jid: str, raw_text: str) -> None:
                channel = app._find_channel(jid)
                if channel:
                    text = format_outbound(channel, raw_text)
                    if text:
                        await channel.send_message(jid, text)

        return _Deps()

    def _make_ipc_deps(self) -> Any:
        """Create the dependency object for the IPC watcher."""
        app = self

        class _Deps:
            async def send_message(self, jid: str, text: str) -> None:
                channel = app._find_channel(jid)
                if channel:
                    await channel.send_message(jid, text)

            def registered_groups(self) -> dict[str, RegisteredGroup]:
                return app.registered_groups

            def register_group(self, jid: str, group: RegisteredGroup) -> None:
                asyncio.ensure_future(app._register_group(jid, group))

            async def sync_group_metadata(self, force: bool) -> None:
                for channel in app.channels:
                    if hasattr(channel, "sync_group_metadata"):
                        await channel.sync_group_metadata(force)

            async def get_available_groups(self) -> list[Any]:
                return await app.get_available_groups()

            def write_groups_snapshot(
                self,
                group_folder: str,
                is_main: bool,
                available_groups: list[Any],
                registered_jids: set[str],
            ) -> None:
                write_groups_snapshot(group_folder, is_main, available_groups, registered_jids)

        return _Deps()
