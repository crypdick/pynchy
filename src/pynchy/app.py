"""Main orchestrator — wires all subsystems together.

Port of src/index.ts. Module-level globals become instance state on PynchyApp.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pynchy.config import (
    ASSISTANT_NAME,
    CONTAINER_IMAGE,
    CONTEXT_RESET_PATTERN,
    DATA_DIR,
    DEPLOY_PORT,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    POLL_INTERVAL,
    PROJECT_ROOT,
    TRIGGER_PATTERN,
)
from pynchy.container_runner import (
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.db import (
    clear_session,
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
    store_message_direct,
)
from pynchy.event_bus import AgentActivityEvent, EventBus, MessageEvent
from pynchy.group_queue import GroupQueue
from pynchy.http_server import start_http_server
from pynchy.ipc import start_ipc_watcher
from pynchy.logger import logger
from pynchy.router import format_messages, format_outbound
from pynchy.runtime import get_runtime
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
        self.event_bus: EventBus = EventBus()
        self._shutting_down: bool = False
        self._http_runner: Any | None = None

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
    # First-run setup
    # ------------------------------------------------------------------

    async def _setup_main_group(self, whatsapp: Any) -> None:
        """Create a new WhatsApp group and register it as the main channel.

        Called on first run when no groups are registered. Creates a private
        group so the user has a dedicated space to talk to the agent.
        """
        group_name = ASSISTANT_NAME.title()
        logger.info("No groups registered. Creating WhatsApp group...", name=group_name)

        jid = await whatsapp.create_group(group_name)

        group = RegisteredGroup(
            name=group_name,
            folder=MAIN_GROUP_FOLDER,
            trigger=f"@{ASSISTANT_NAME}",
            added_at=datetime.now(UTC).isoformat(),
            requires_trigger=False,
        )
        await self._register_group(jid, group)
        logger.info(
            "Main channel created! Open the group in WhatsApp to start chatting.",
            group=group_name,
            jid=jid,
        )

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _process_group_messages(self, chat_jid: str) -> bool:
        """Process all pending messages for a group. Called by GroupQueue."""
        group = self.registered_groups.get(chat_jid)
        if not group:
            return True

        # Check for agent-initiated context reset prompt
        reset_file = DATA_DIR / "ipc" / group.folder / "reset_prompt.json"
        if reset_file.exists():
            try:
                reset_data = json.loads(reset_file.read_text())
                reset_file.unlink()
            except Exception:
                reset_file.unlink(missing_ok=True)
                return True

            prompt = reset_data.get("message", "")
            if prompt:
                logger.info("Processing reset handoff", group=group.name)
                result = await self._run_agent(group, prompt, chat_jid)
                return result != "error"
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

        # Check if the last message is a context reset command
        if CONTEXT_RESET_PATTERN.match(missed_messages[-1].content.strip()):
            self.sessions.pop(group.folder, None)
            await clear_session(group.folder)
            self.queue.close_stdin(chat_jid)
            self.last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
            await self._save_state()
            reset_text = "Context reset. Next message starts a fresh session."
            for ch in self.channels:
                if ch.is_connected():
                    with contextlib.suppress(Exception):
                        await ch.send_message(chat_jid, reset_text)
            logger.info("Context reset", group=group.name)
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
            preview=missed_messages[-1].content[:200],
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

        # Set typing indicator on all channels that support it
        for ch in self.channels:
            if ch.is_connected() and hasattr(ch, "set_typing"):
                await ch.set_typing(chat_jid, True)

        self.event_bus.emit(AgentActivityEvent(chat_jid=chat_jid, active=True))

        had_error = False
        output_sent_to_user = False

        async def on_output(result: ContainerOutput) -> None:
            nonlocal had_error, output_sent_to_user
            if result.result:
                raw = result.result if isinstance(result.result, str) else json.dumps(result.result)
                from pynchy.router import strip_internal_tags

                text = strip_internal_tags(raw)
                logger.info("Agent output", group=group.name, text=raw[:200])
                if text:
                    formatted = f"{ASSISTANT_NAME}: {text}"
                    # Store bot response in SQLite (source of truth)
                    ts = datetime.now(UTC).isoformat()
                    await store_message_direct(
                        id=f"bot-{int(datetime.now(UTC).timestamp() * 1000)}",
                        chat_jid=chat_jid,
                        sender="bot",
                        sender_name=ASSISTANT_NAME,
                        content=formatted,
                        timestamp=ts,
                        is_from_me=True,
                    )
                    # Broadcast to all connected channels
                    for ch in self.channels:
                        if ch.is_connected():
                            try:
                                await ch.send_message(chat_jid, formatted)
                            except Exception as exc:
                                logger.warning("Channel send failed", channel=ch.name, err=str(exc))
                    # Emit for real-time TUI updates
                    self.event_bus.emit(
                        MessageEvent(
                            chat_jid=chat_jid,
                            sender_name=ASSISTANT_NAME,
                            content=formatted,
                            timestamp=ts,
                            is_bot=True,
                        )
                    )
                    output_sent_to_user = True
                # Only reset idle timer on actual results, not session-update markers
                reset_idle_timer()

            if result.status == "error":
                had_error = True

        agent_result = await self._run_agent(group, prompt, chat_jid, on_output)

        for ch in self.channels:
            if ch.is_connected() and hasattr(ch, "set_typing"):
                await ch.set_typing(chat_jid, False)
        self.event_bus.emit(AgentActivityEvent(chat_jid=chat_jid, active=False))
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

        while not self._shutting_down:
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
                                TRIGGER_PATTERN.search(m.content.strip()) for m in group_messages
                            )
                            if not has_trigger:
                                continue

                        # Pull all messages since lastAgentTimestamp for context
                        all_pending = await get_messages_since(
                            chat_jid,
                            self.last_agent_timestamp.get(chat_jid, ""),
                            ASSISTANT_NAME,
                        )
                        if not all_pending:
                            # Already consumed by _process_group_messages
                            continue
                        formatted = format_messages(all_pending)

                        if self.queue.send_message(chat_jid, formatted):
                            logger.debug(
                                "Piped messages to active container",
                                chat_jid=chat_jid,
                                count=len(all_pending),
                            )
                            self.last_agent_timestamp[chat_jid] = all_pending[-1].timestamp
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
    # Deploy rollback
    # ------------------------------------------------------------------

    async def _auto_rollback(self, continuation_path: Path, exc: Exception) -> None:
        """Roll back to the previous commit if startup fails after a deploy."""
        try:
            continuation = json.loads(continuation_path.read_text())
        except Exception:
            logger.error("Failed to read continuation for rollback")
            return

        previous_sha = continuation.get("previous_commit_sha", "")
        if not previous_sha:
            logger.warning("No previous_commit_sha in continuation, cannot rollback")
            return

        logger.warning(
            "Startup failed after deploy, rolling back",
            previous_sha=previous_sha,
            error=str(exc),
        )

        result = subprocess.run(
            ["git", "reset", "--hard", previous_sha],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Rollback git reset failed", stderr=result.stderr)
            return

        # Rewrite continuation with rollback info (clear previous_commit_sha to prevent loops)
        error_short = str(exc)[:200]
        continuation["resume_prompt"] = (
            f"ROLLBACK: Startup failed ({error_short}). Rolled back to {previous_sha[:8]}."
        )
        continuation["previous_commit_sha"] = ""
        continuation_path.write_text(json.dumps(continuation, indent=2))

        logger.info("Rollback complete, exiting for service restart")
        import sys

        sys.exit(1)

    # ------------------------------------------------------------------
    # Deploy continuation
    # ------------------------------------------------------------------

    async def _check_deploy_continuation(self) -> None:
        """Check for a deploy continuation file and inject a resume message."""
        continuation_path = DATA_DIR / "deploy_continuation.json"
        if not continuation_path.exists():
            return

        try:
            continuation = json.loads(continuation_path.read_text())
            continuation_path.unlink()
        except Exception as exc:
            logger.error("Failed to read deploy continuation", err=str(exc))
            return

        chat_jid = continuation.get("chat_jid", "")
        resume_prompt = continuation.get("resume_prompt", "Deploy complete.")
        commit_sha = continuation.get("commit_sha", "unknown")

        if not chat_jid:
            logger.warning("Deploy continuation missing chat_jid, skipping")
            return

        logger.info(
            "Deploy continuation found, injecting resume message",
            commit_sha=commit_sha,
            chat_jid=chat_jid,
        )

        # Inject a synthetic message to resume the agent session
        synthetic_msg = NewMessage(
            id=f"deploy-{commit_sha[:8]}-{int(datetime.now(UTC).timestamp() * 1000)}",
            chat_jid=chat_jid,
            sender="system",
            sender_name="system",
            content=f"[DEPLOY COMPLETE — {commit_sha[:8]}] {resume_prompt}",
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
        )
        await store_message(synthetic_msg)
        self.queue.enqueue_message_check(chat_jid)

    # ------------------------------------------------------------------
    # Container system
    # ------------------------------------------------------------------

    def _ensure_container_system_running(self) -> None:
        """Verify container runtime is available and stop orphaned containers."""
        runtime = get_runtime()
        runtime.ensure_running()

        # Auto-build container image if missing
        result = subprocess.run(
            [runtime.cli, "image", "inspect", CONTAINER_IMAGE],
            capture_output=True,
        )
        if result.returncode != 0:
            from pynchy.config import PROJECT_ROOT

            container_dir = PROJECT_ROOT / "container"
            if not (container_dir / "Dockerfile").exists():
                raise RuntimeError(
                    f"Container image '{CONTAINER_IMAGE}' not found and "
                    f"no Dockerfile at {container_dir / 'Dockerfile'}"
                )
            logger.info("Container image not found, building...", image=CONTAINER_IMAGE)
            build = subprocess.run(
                [runtime.cli, "build", "-t", CONTAINER_IMAGE, "."],
                cwd=str(container_dir),
            )
            if build.returncode != 0:
                raise RuntimeError(f"Failed to build container image '{CONTAINER_IMAGE}'")

        # Kill orphaned containers from previous runs
        orphans = runtime.list_running_containers("pynchy-")
        for name in orphans:
            with contextlib.suppress(Exception):
                subprocess.run(
                    [runtime.cli, "stop", name],
                    capture_output=True,
                )
        if orphans:
            logger.info(
                "Stopped orphaned containers",
                count=len(orphans),
                names=orphans,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_channel(self, jid: str) -> Channel | None:
        """Find the channel that owns a given JID."""
        for c in self.channels:
            if c.owns_jid(jid):
                return c
        return None

    async def _on_inbound(self, _jid: str, msg: NewMessage) -> None:
        """Handle inbound message from any channel — store, emit, and enqueue."""
        await store_message(msg)
        self.event_bus.emit(
            MessageEvent(
                chat_jid=msg.chat_jid,
                sender_name=msg.sender_name,
                content=msg.content,
                timestamp=msg.timestamp,
                is_bot=False,
            )
        )

    # ------------------------------------------------------------------
    # Tailscale
    # ------------------------------------------------------------------

    @staticmethod
    def _check_tailscale() -> None:
        """Log a warning if Tailscale is not connected. Non-fatal."""
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning("Tailscale not connected (non-fatal)", stderr=result.stderr.strip())
                return
            status = json.loads(result.stdout)
            backend = status.get("BackendState", "")
            if backend != "Running":
                logger.warning("Tailscale backend not running", state=backend)
            else:
                logger.info("Tailscale connected", state=backend)
        except FileNotFoundError:
            logger.warning("Tailscale CLI not found (non-fatal)")
        except Exception as exc:
            logger.warning("Tailscale check failed (non-fatal)", err=str(exc))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _shutdown(self, sig_name: str) -> None:
        """Graceful shutdown handler. Second signal force-exits."""
        if self._shutting_down:
            logger.info("Force shutdown")
            import os

            os._exit(1)
        self._shutting_down = True
        logger.info("Shutdown signal received", signal=sig_name)
        if self._http_runner:
            await self._http_runner.cleanup()
        await self.queue.shutdown(10.0)
        for channel in self.channels:
            await channel.disconnect()

    async def run(self) -> None:
        """Main entry point — startup sequence."""
        continuation_path = DATA_DIR / "deploy_continuation.json"

        try:
            self._ensure_container_system_running()
            await init_database()
            logger.info("Database initialized")
            await self._load_state()
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

        # Create and connect WhatsApp channel
        from pynchy.channels.whatsapp import WhatsAppChannel

        whatsapp = WhatsAppChannel(
            on_message=lambda jid, msg: asyncio.ensure_future(self._on_inbound(jid, msg)),
            on_chat_metadata=lambda jid, ts: asyncio.ensure_future(store_chat_metadata(jid, ts)),
            registered_groups=lambda: self.registered_groups,
        )
        self.channels.append(whatsapp)

        try:
            await whatsapp.connect()
        except Exception as exc:
            if continuation_path.exists():
                await self._auto_rollback(continuation_path, exc)
            raise

        # First-run: create a private group and register as main channel
        if not self.registered_groups:
            await self._setup_main_group(whatsapp)

        # Start subsystems
        asyncio.create_task(start_scheduler_loop(self._make_scheduler_deps()))
        asyncio.create_task(start_ipc_watcher(self._make_ipc_deps()))
        self.queue.set_process_messages_fn(self._process_group_messages)

        # HTTP server for remote health checks, deploys, and TUI API
        self._check_tailscale()
        self._http_runner = await start_http_server(self._make_http_deps())
        logger.info("HTTP server ready", port=DEPLOY_PORT)

        await self._recover_pending_messages()
        await self._check_deploy_continuation()
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
                for ch in app.channels:
                    if ch.is_connected():
                        text = format_outbound(ch, raw_text)
                        if text:
                            with contextlib.suppress(Exception):
                                await ch.send_message(jid, text)

        return _Deps()

    def _make_http_deps(self) -> Any:
        """Create the dependency object for the HTTP server."""
        app = self

        class _Deps:
            async def send_message(self, jid: str, text: str) -> None:
                for ch in app.channels:
                    if ch.is_connected():
                        with contextlib.suppress(Exception):
                            await ch.send_message(jid, text)

            def main_chat_jid(self) -> str:
                for jid, group in app.registered_groups.items():
                    if group.folder == MAIN_GROUP_FOLDER:
                        return jid
                return ""

            def channels_connected(self) -> bool:
                return any(c.is_connected() for c in app.channels)

            # --- TUI API deps ---

            def get_groups(self) -> list[dict[str, Any]]:
                return [
                    {"jid": jid, "name": g.name, "folder": g.folder}
                    for jid, g in app.registered_groups.items()
                ]

            async def get_messages(self, jid: str, limit: int) -> list[NewMessage]:
                from pynchy.db import get_chat_history

                return await get_chat_history(jid, limit)

            async def send_user_message(self, jid: str, content: str) -> None:
                msg = NewMessage(
                    id=f"tui-{int(datetime.now(UTC).timestamp() * 1000)}",
                    chat_jid=jid,
                    sender="tui-user",
                    sender_name="You",
                    content=content,
                    timestamp=datetime.now(UTC).isoformat(),
                    is_from_me=False,
                )
                await store_message(msg)
                app.event_bus.emit(
                    MessageEvent(
                        chat_jid=jid,
                        sender_name="You",
                        content=content,
                        timestamp=msg.timestamp,
                        is_bot=False,
                    )
                )
                app.queue.enqueue_message_check(jid)

            def subscribe_events(self, callback: Any) -> Any:
                from pynchy.event_bus import AgentActivityEvent, MessageEvent

                unsubs = []

                async def on_msg(event: MessageEvent) -> None:
                    await callback(
                        {
                            "type": "message",
                            "chat_jid": event.chat_jid,
                            "sender_name": event.sender_name,
                            "content": event.content,
                            "timestamp": event.timestamp,
                            "is_bot": event.is_bot,
                        }
                    )

                async def on_activity(event: AgentActivityEvent) -> None:
                    await callback(
                        {
                            "type": "agent_activity",
                            "chat_jid": event.chat_jid,
                            "active": event.active,
                        }
                    )

                unsubs.append(app.event_bus.subscribe(MessageEvent, on_msg))
                unsubs.append(app.event_bus.subscribe(AgentActivityEvent, on_activity))

                def unsubscribe() -> None:
                    for unsub in unsubs:
                        unsub()

                return unsubscribe

        return _Deps()

    def _make_ipc_deps(self) -> Any:
        """Create the dependency object for the IPC watcher."""
        app = self

        class _Deps:
            async def send_message(self, jid: str, text: str) -> None:
                for ch in app.channels:
                    if ch.is_connected():
                        with contextlib.suppress(Exception):
                            await ch.send_message(jid, text)

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

            async def clear_session(self, group_folder: str) -> None:
                app.sessions.pop(group_folder, None)
                await clear_session(group_folder)

            def enqueue_message_check(self, group_jid: str) -> None:
                app.queue.enqueue_message_check(group_jid)

        return _Deps()
