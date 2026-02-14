"""Main orchestrator — wires all subsystems together.

Port of src/index.ts. Module-level globals become instance state on PynchyApp.
"""

from __future__ import annotations

import asyncio
import contextlib
import filecmp
import json
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pynchy.config import (
    ASSISTANT_NAME,
    CONTAINER_IMAGE,
    DATA_DIR,
    DEPLOY_PORT,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    POLL_INTERVAL,
    PROJECT_ROOT,
    TRIGGER_PATTERN,
    is_context_reset,
)
from pynchy.container_runner import (
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.db import (
    clear_session,
    create_task,
    get_active_task_for_group,
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    init_database,
    set_chat_cleared_at,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
    store_message_direct,
    update_task,
)
from pynchy.event_bus import (
    AgentActivityEvent,
    AgentTraceEvent,
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)
from pynchy.group_queue import GroupQueue
from pynchy.http_server import (
    _get_head_commit_message,
    _get_head_sha,
    _is_repo_dirty,
    _push_local_commits,
    start_http_server,
)
from pynchy.ipc import start_ipc_watcher
from pynchy.logger import logger
from pynchy.router import format_messages, format_outbound, format_tool_preview, parse_host_tag
from pynchy.runtime import get_runtime
from pynchy.task_scheduler import start_scheduler_loop
from pynchy.types import Channel, ContainerInput, ContainerOutput, NewMessage, RegisteredGroup

_trace_counter = 0


def _next_trace_id(prefix: str) -> str:
    global _trace_counter
    _trace_counter += 1
    ts_ms = int(datetime.now(UTC).timestamp() * 1000)
    return f"{prefix}-{ts_ms}-{_trace_counter}"


class PynchyApp:
    """Main application class — owns all runtime state and wires subsystems."""

    def __init__(self) -> None:
        self.last_timestamp: str = ""
        self.sessions: dict[str, str] = {}
        self._session_cleared: set[str] = set()  # group folders with pending clears
        self.registered_groups: dict[str, RegisteredGroup] = {}
        self.last_agent_timestamp: dict[str, str] = {}
        self.message_loop_running: bool = False
        self.queue: GroupQueue = GroupQueue()
        self.channels: list[Channel] = []
        self.event_bus: EventBus = EventBus()
        self._shutting_down: bool = False
        self._http_runner: Any | None = None
        self.registry: Any = None  # PluginRegistry, set during startup

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

    async def _reconcile_periodic_agents(self) -> None:
        """Scan groups/ for periodic.yaml files and ensure tasks + chat groups exist.

        Idempotent — safe to run on every startup. Creates WhatsApp groups for
        new periodic agents, and updates scheduled tasks if config changed.
        """
        import uuid
        from zoneinfo import ZoneInfo

        from croniter import croniter

        from pynchy.config import TIMEZONE
        from pynchy.periodic import load_periodic_config

        # Build folder->jid lookup from existing registered groups
        folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in self.registered_groups.items()}

        # Scan all group folders for periodic.yaml
        if not GROUPS_DIR.exists():
            return

        reconciled = 0
        for folder in sorted(GROUPS_DIR.iterdir()):
            if not folder.is_dir():
                continue

            config = load_periodic_config(folder.name)
            if config is None:
                continue

            # 1. Ensure the group is registered (create chat group if needed)
            jid = folder_to_jid.get(folder.name)
            if jid is None:
                # Find a channel that supports create_group
                channel = next(
                    (ch for ch in self.channels if hasattr(ch, "create_group")),
                    None,
                )
                if channel is None:
                    logger.warning(
                        "No channel supports create_group, skipping periodic agent",
                        folder=folder.name,
                    )
                    continue

                agent_name = folder.name.replace("-", " ").title()
                jid = await channel.create_group(agent_name)
                group = RegisteredGroup(
                    name=agent_name,
                    folder=folder.name,
                    trigger=f"@{ASSISTANT_NAME}",
                    added_at=datetime.now(UTC).isoformat(),
                    requires_trigger=False,
                )
                await self._register_group(jid, group)
                folder_to_jid[folder.name] = jid
                logger.info(
                    "Created chat group for periodic agent",
                    name=agent_name,
                    folder=folder.name,
                )

            # 2. Ensure a scheduled task exists and is up to date
            existing_task = await get_active_task_for_group(folder.name)

            if existing_task is None:
                # Create new task
                tz = ZoneInfo(TIMEZONE)
                cron = croniter(config.schedule, datetime.now(tz))
                next_run = cron.get_next(datetime).isoformat()

                task_id = f"periodic-{folder.name}-{uuid.uuid4().hex[:8]}"
                await create_task(
                    {
                        "id": task_id,
                        "group_folder": folder.name,
                        "chat_jid": jid,
                        "prompt": config.prompt,
                        "schedule_type": "cron",
                        "schedule_value": config.schedule,
                        "context_mode": config.context_mode,
                        "project_access": config.project_access,
                        "next_run": next_run,
                        "status": "active",
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
                logger.info(
                    "Created scheduled task for periodic agent",
                    task_id=task_id,
                    folder=folder.name,
                    schedule=config.schedule,
                )
            else:
                # Update if schedule or prompt changed
                updates: dict[str, Any] = {}
                if existing_task.schedule_value != config.schedule:
                    updates["schedule_value"] = config.schedule
                    tz = ZoneInfo(TIMEZONE)
                    cron = croniter(config.schedule, datetime.now(tz))
                    updates["next_run"] = cron.get_next(datetime).isoformat()
                if existing_task.prompt != config.prompt:
                    updates["prompt"] = config.prompt
                if existing_task.project_access != config.project_access:
                    updates["project_access"] = config.project_access
                if updates:
                    await update_task(existing_task.id, updates)
                    logger.info(
                        "Updated periodic agent task",
                        task_id=existing_task.id,
                        folder=folder.name,
                        changed=list(updates.keys()),
                    )

            reconciled += 1

        if reconciled:
            logger.info("Periodic agents reconciled", count=reconciled)

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

    async def _connect_plugin_channels(self) -> None:
        """Create and connect channels from plugins.

        Called during startup after WhatsApp is connected. Creates a channel
        for each ChannelPlugin and connects it.
        """
        from pynchy.plugin import PluginContext

        if not self.registry or not self.registry.channels:
            logger.debug("No channel plugins found")
            return

        # Create plugin context for channel initialization
        ctx = PluginContext(
            registered_groups=lambda: self.registered_groups,
            send_message=self._plugin_send_message,
        )

        for plugin in self.registry.channels:
            try:
                # Validate required credentials
                missing_creds = self._validate_plugin_credentials(plugin)
                if missing_creds:
                    logger.warning(
                        "Channel plugin missing required credentials, skipping",
                        plugin=plugin.name,
                        missing=missing_creds,
                    )
                    continue

                # Create channel
                channel = plugin.create_channel(ctx)
                self.channels.append(channel)

                # Connect
                await channel.connect()
                logger.info(
                    "Plugin channel connected",
                    plugin=plugin.name,
                    channel=channel.name,
                )

            except Exception as exc:
                logger.error(
                    "Failed to connect plugin channel",
                    plugin=plugin.name,
                    error=str(exc),
                )
                # Continue with other plugins - don't crash startup

    async def _plugin_send_message(self, jid: str, text: str) -> None:
        """Send message helper for plugin context.

        Sends to all connected channels (used by plugins that need to
        broadcast messages).
        """
        await self._broadcast_to_channels(jid, text)

    def _validate_plugin_credentials(self, plugin: Any) -> list[str]:
        """Check if plugin has required environment variables.

        Args:
            plugin: Plugin instance with optional requires_credentials() method

        Returns:
            List of missing credential names (empty if all present)
        """
        import os

        if not hasattr(plugin, "requires_credentials"):
            return []

        required = plugin.requires_credentials()
        missing = [cred for cred in required if cred not in os.environ]
        return missing

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
                await self._broadcast_host_message(chat_jid, f"[handoff] {prompt}")

                async def handoff_on_output(result: ContainerOutput) -> None:
                    await self._handle_streamed_output(chat_jid, group, result)

                result = await self._run_agent(group, prompt, chat_jid, handoff_on_output)
                return result != "error"
            return True

        is_main_group = group.folder == MAIN_GROUP_FOLDER
        since_timestamp = self.last_agent_timestamp.get(chat_jid, "")
        missed_messages = await get_messages_since(chat_jid, since_timestamp)

        if not missed_messages:
            return True

        # For non-main groups, check if trigger is required and present
        if not is_main_group and group.requires_trigger is not False:
            has_trigger = any(TRIGGER_PATTERN.search(m.content.strip()) for m in missed_messages)
            if not has_trigger:
                return True

        # Check if the last message is a context reset command
        if is_context_reset(missed_messages[-1].content):
            self.sessions.pop(group.folder, None)
            self._session_cleared.add(group.folder)
            await clear_session(group.folder)
            self.queue.close_stdin(chat_jid)
            self.last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
            await self._save_state()
            await self._send_clear_confirmation(chat_jid)
            logger.info("Context reset", group=group.name)
            return True

        # Check if the last message is a direct command execution (!command syntax)
        last_msg_content = missed_messages[-1].content.strip()
        if last_msg_content.startswith("!"):
            command = last_msg_content[1:]  # Remove the ! prefix
            if command:
                await self._execute_direct_command(chat_jid, group, missed_messages[-1], command)
                # Advance cursor but don't trigger agent
                self.last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
                await self._save_state()
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

        # Send emoji reaction on the last message to indicate agent is reading
        last_msg = missed_messages[-1]
        await self._send_reaction_to_channels(chat_jid, last_msg.id, last_msg.sender, "👀")

        # Set typing indicator on all channels that support it
        await self._set_typing_on_channels(chat_jid, True)

        self.event_bus.emit(AgentActivityEvent(chat_jid=chat_jid, active=True))

        had_error = False
        output_sent_to_user = False

        async def on_output(result: ContainerOutput) -> None:
            nonlocal had_error, output_sent_to_user

            sent = await self._handle_streamed_output(chat_jid, group, result)
            if sent:
                output_sent_to_user = True
            # Only reset idle timer on actual results, not session-update markers
            if result.type == "result":
                reset_idle_timer()
            if result.status == "error":
                had_error = True

        agent_result = await self._run_agent(group, prompt, chat_jid, on_output)

        await self._set_typing_on_channels(chat_jid, False)
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
            # Send error notification to user
            await self._broadcast_host_message(
                chat_jid, "⚠️ Agent error occurred. Will retry on next message."
            )
            # Roll back cursor for retry
            self.last_agent_timestamp[chat_jid] = previous_cursor
            await self._save_state()
            logger.warning(
                "Agent error, rolled back message cursor for retry",
                group=group.name,
            )
            return False

        # Push local commits after successful agent session (main group only)
        if is_main_group:
            asyncio.create_task(asyncio.to_thread(_push_local_commits))

        return True

    async def _execute_direct_command(
        self, chat_jid: str, group: RegisteredGroup, message: NewMessage, command: str
    ) -> None:
        """Execute a user command directly without LLM approval.

        Stores both the command and its output in the message history so the LLM
        can see it when triggered by a subsequent message.
        """
        logger.info("Executing direct command", group=group.name, command=command[:100])

        try:
            # Execute command with a timeout
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(GROUPS_DIR / group.folder),
            )

            # Format output
            if result.returncode == 0:
                output = result.stdout if result.stdout else "(no output)"
                status_emoji = "✅"
            else:
                output = result.stderr if result.stderr else result.stdout or "(no output)"
                status_emoji = "❌"

            # Store the command output as a system message
            ts = datetime.now(UTC).isoformat()
            output_text = (
                f"{status_emoji} Command output (exit {result.returncode}):\n```\n{output}\n```"
            )

            await store_message_direct(
                id=f"cmd-{int(datetime.now(UTC).timestamp() * 1000)}",
                chat_jid=chat_jid,
                sender="command_output",
                sender_name="command",
                content=output_text,
                timestamp=ts,
                is_from_me=True,
            )

            # Send to channels
            channel_text = f"🔧 {output_text}"
            await self._broadcast_to_channels(chat_jid, channel_text)

            # Emit event for TUI
            self.event_bus.emit(
                MessageEvent(
                    chat_jid=chat_jid,
                    sender_name="command",
                    content=output_text,
                    timestamp=ts,
                    is_bot=True,
                )
            )

            logger.info(
                "Direct command executed",
                group=group.name,
                exit_code=result.returncode,
                output_len=len(output),
            )

        except subprocess.TimeoutExpired:
            error_msg = "⏱️ Command timed out (30s limit)"
            await self._broadcast_host_message(chat_jid, error_msg)
            logger.warning("Direct command timeout", group=group.name, command=command[:100])
        except Exception as exc:
            error_msg = f"❌ Command failed: {str(exc)}"
            await self._broadcast_host_message(chat_jid, error_msg)
            logger.error("Direct command error", group=group.name, error=str(exc))

    async def _broadcast_trace(
        self,
        chat_jid: str,
        trace_type: str,
        data: dict[str, Any],
        channel_text: str,
        *,
        db_id_prefix: str,
        db_sender: str,
    ) -> None:
        """Store a trace event, send to channels, and emit to EventBus."""
        ts = datetime.now(UTC).isoformat()
        await store_message_direct(
            id=_next_trace_id(db_id_prefix),
            chat_jid=chat_jid,
            sender=db_sender,
            sender_name=db_sender,
            content=json.dumps(data),
            timestamp=ts,
            is_from_me=True,
        )
        await self._broadcast_to_channels(chat_jid, channel_text)
        self.event_bus.emit(AgentTraceEvent(chat_jid=chat_jid, trace_type=trace_type, data=data))

    async def _handle_streamed_output(
        self, chat_jid: str, group: RegisteredGroup, result: ContainerOutput
    ) -> bool:
        """Handle a streamed output from the container agent.

        Broadcasts trace events and results to channels/TUI.
        Returns True if a user-visible result was sent.
        """
        from pynchy.router import strip_internal_tags

        ts = datetime.now(UTC).isoformat()

        # --- Trace events: persist to DB + broadcast ---
        if result.type == "thinking":
            await self._broadcast_trace(
                chat_jid,
                "thinking",
                {"thinking": result.thinking or ""},
                "\U0001f4ad thinking...",
                db_id_prefix="think",
                db_sender="thinking",
            )
            return False
        if result.type == "tool_use":
            tool_name = result.tool_name or "tool"
            tool_input = result.tool_input or {}
            data = {"tool_name": tool_name, "tool_input": tool_input}
            preview = format_tool_preview(tool_name, tool_input)
            await self._broadcast_trace(
                chat_jid,
                "tool_use",
                data,
                f"\U0001f527 {preview}",
                db_id_prefix="tool",
                db_sender="tool_use",
            )
            return False
        if result.type == "tool_result":
            await self._broadcast_trace(
                chat_jid,
                "tool_result",
                {
                    "tool_use_id": result.tool_result_id or "",
                    "content": result.tool_result_content or "",
                    "is_error": result.tool_result_is_error or False,
                },
                "\U0001f4cb tool result",
                db_id_prefix="toolr",
                db_sender="tool_result",
            )
            return False
        if result.type == "system":
            await self._broadcast_trace(
                chat_jid,
                "system",
                {
                    "subtype": result.system_subtype or "",
                    "data": result.system_data or {},
                },
                f"\u2699\ufe0f system: {result.system_subtype or 'unknown'}",
                db_id_prefix="sys",
                db_sender="system",
            )
            return False
        if result.type == "text":
            self.event_bus.emit(
                AgentTraceEvent(
                    chat_jid=chat_jid,
                    trace_type="text",
                    data={"text": result.text or ""},
                )
            )
            return False

        # Persist result metadata if present (cost, usage, duration)
        if result.result_metadata:
            meta = result.result_metadata
            await store_message_direct(
                id=_next_trace_id("meta"),
                chat_jid=chat_jid,
                sender="result_meta",
                sender_name="result_meta",
                content=json.dumps(meta),
                timestamp=ts,
                is_from_me=True,
            )
            cost = meta.get("total_cost_usd")
            duration = meta.get("duration_ms")
            turns = meta.get("num_turns")
            parts = []
            if cost is not None:
                parts.append(f"{cost:.2f} USD")
            if duration is not None:
                parts.append(f"{duration / 1000:.1f}s")
            if turns is not None:
                parts.append(f"{turns} turns")
            if parts:
                trace_text = f"\U0001f4ca {' \u00b7 '.join(parts)}"
                await self._broadcast_to_channels(chat_jid, trace_text)
            self.event_bus.emit(
                AgentTraceEvent(
                    chat_jid=chat_jid,
                    trace_type="result_meta",
                    data=meta,
                )
            )

        if result.result:
            raw = result.result if isinstance(result.result, str) else json.dumps(result.result)
            text = strip_internal_tags(raw)
            if text:
                is_host, content = parse_host_tag(text)
                if is_host:
                    sender = "host"
                    sender_name = "host"
                    db_content = content
                    channel_text = f"\U0001f3e0 {content}"
                    logger.info("Host message", group=group.name, text=content[:200])
                else:
                    sender = "bot"
                    sender_name = ASSISTANT_NAME
                    db_content = text
                    channel_text = f"{ASSISTANT_NAME}: {text}"
                    logger.info("Agent output", group=group.name, text=raw[:200])
                await store_message_direct(
                    id=f"bot-{int(datetime.now(UTC).timestamp() * 1000)}",
                    chat_jid=chat_jid,
                    sender=sender,
                    sender_name=sender_name,
                    content=db_content,
                    timestamp=ts,
                    is_from_me=True,
                )
                await self._broadcast_to_channels(chat_jid, channel_text, suppress_errors=False)
                self.event_bus.emit(
                    MessageEvent(
                        chat_jid=chat_jid,
                        sender_name=sender_name,
                        content=db_content,
                        timestamp=ts,
                        is_bot=True,
                    )
                )
                return True

        return False

    async def _run_agent(
        self,
        group: RegisteredGroup,
        prompt: str,
        chat_jid: str,
        on_output: Any | None = None,
    ) -> str:
        """Run the container agent for a group. Returns 'success' or 'error'."""
        from pynchy.periodic import load_periodic_config

        is_main = group.folder == MAIN_GROUP_FOLDER
        periodic_config = load_periodic_config(group.folder)
        project_access = periodic_config.project_access if periodic_config else False
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
            if output.new_session_id and group.folder not in self._session_cleared:
                self.sessions[group.folder] = output.new_session_id
                await set_session(group.folder, output.new_session_id)
            if on_output:
                await on_output(output)

        # Warn the agent about repo issues so it can self-correct
        system_notices: list[str] = []
        if is_main:
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                system_notices.append(
                    "There are uncommitted local changes. Run `git status` and `git diff` "
                    "to review them. If they are good, commit and push. If not, discard them."
                )
            unpushed = subprocess.run(
                ["git", "rev-list", "origin/main..HEAD", "--count"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if unpushed.returncode == 0 and int(unpushed.stdout.strip() or "0") > 0:
                system_notices.append(
                    "There are local commits that haven't been pushed. "
                    "Run `git push` or `git rebase origin/main && git push` to sync them."
                )
            if system_notices:
                system_notices.append(
                    "Consider whether to address these issues "
                    "before or after handling the new message."
                )

        # Clear the guard — this container run starts fresh
        self._session_cleared.discard(group.folder)

        try:
            output = await run_container_agent(
                group=group,
                input_data=ContainerInput(
                    prompt=prompt,
                    session_id=session_id,
                    group_folder=group.folder,
                    chat_jid=chat_jid,
                    is_main=is_main,
                    system_notices=system_notices or None,
                    project_access=project_access,
                ),
                on_process=lambda proc, name: self.queue.register_process(
                    chat_jid, proc, name, group.folder
                ),
                on_output=wrapped_on_output if on_output else None,
                registry=self.registry,
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
        """Main polling loop — checks for new messages every POLL_INTERVAL."""
        if self.message_loop_running:
            logger.debug("Message loop already running, skipping duplicate start")
            return
        self.message_loop_running = True

        logger.info(f"Pynchy running (trigger: @{ASSISTANT_NAME})")

        while not self._shutting_down:
            try:
                jids = list(self.registered_groups.keys())
                messages, new_timestamp = await get_new_messages(jids, self.last_timestamp)

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
                        )
                        if not all_pending:
                            # Already consumed by _process_group_messages
                            continue

                        # Intercept context reset commands before piping to
                        # active containers — they must be handled by the host,
                        # not forwarded as regular user messages.
                        if is_context_reset(all_pending[-1].content):
                            self.sessions.pop(group.folder, None)
                            self._session_cleared.add(group.folder)
                            await clear_session(group.folder)
                            self.queue.close_stdin(chat_jid)
                            self.last_agent_timestamp[chat_jid] = all_pending[-1].timestamp
                            await self._save_state()
                            await self._send_clear_confirmation(chat_jid)
                            logger.info("Context reset (active container)", group=group.name)
                            continue

                        formatted = format_messages(all_pending)

                        if self.queue.send_message(chat_jid, formatted):
                            logger.debug(
                                "Piped messages to active container",
                                chat_jid=chat_jid,
                                count=len(all_pending),
                            )
                            # Send emoji reaction to indicate reading
                            last_msg = all_pending[-1]
                            await self._send_reaction_to_channels(
                                chat_jid, last_msg.id, last_msg.sender, "👀"
                            )

                            self.last_agent_timestamp[chat_jid] = all_pending[-1].timestamp
                            await self._save_state()
                        else:
                            self.queue.enqueue_message_check(chat_jid)

            except Exception as exc:
                logger.error("Error in message loop", err=str(exc))

            await asyncio.sleep(POLL_INTERVAL)

    async def _send_boot_notification(self) -> None:
        """Send a system message to the main channel on startup."""
        main_jid = next(
            (jid for jid, g in self.registered_groups.items() if g.folder == MAIN_GROUP_FOLDER),
            None,
        )
        if not main_jid:
            return

        sha = _get_head_sha()[:8]
        commit_msg = _get_head_commit_message(50)
        dirty = " (dirty)" if _is_repo_dirty() else ""
        label = f"{sha}{dirty} {commit_msg}".strip() if commit_msg else f"{sha}{dirty}"
        parts = [f"{ASSISTANT_NAME} online — {label}"]

        # Check for API credentials and warn if missing
        from pynchy.container_runner import _write_env_file

        if _write_env_file() is None:
            parts.append(
                "⚠️ No API credentials found — messages will fail. "
                "Run 'claude' to authenticate or set ANTHROPIC_API_KEY in .env"
            )
            logger.warning("No API credentials found at startup")

        # Check for boot warnings left by a previous deploy
        boot_warnings_path = DATA_DIR / "boot_warnings.json"
        if boot_warnings_path.exists():
            try:
                warnings = json.loads(boot_warnings_path.read_text())
                boot_warnings_path.unlink()
                for warning in warnings:
                    parts.append(f"⚠️ {warning}")
            except Exception:
                boot_warnings_path.unlink(missing_ok=True)

        await self._broadcast_host_message(main_jid, "\n".join(parts))
        logger.info("Boot notification sent")

    async def _recover_pending_messages(self) -> None:
        """Startup recovery: check for unprocessed messages in registered groups."""
        for chat_jid, group in self.registered_groups.items():
            since_timestamp = self.last_agent_timestamp.get(chat_jid, "")
            pending = await get_messages_since(chat_jid, since_timestamp)
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
        session_id = continuation.get("session_id", "")
        resume_prompt = continuation.get("resume_prompt", "Deploy complete.")
        commit_sha = continuation.get("commit_sha", "unknown")

        if not chat_jid:
            logger.warning("Deploy continuation missing chat_jid, skipping")
            return

        # Only inject a resume message if an agent session needs to continue.
        # Plain HTTP deploys have no session_id — the boot notification suffices.
        if not session_id:
            logger.info(
                "Deploy continuation has no session_id, skipping agent resume",
                commit_sha=commit_sha,
            )
            return

        logger.info(
            "Deploy continuation found, injecting resume message",
            commit_sha=commit_sha,
            chat_jid=chat_jid,
        )

        # Inject a synthetic message to resume the agent session.
        # Uses sender="deploy" so it passes get_messages_since filters
        # (sender="host" is excluded to prevent host messages triggering the agent).
        synthetic_msg = NewMessage(
            id=f"deploy-{commit_sha[:8]}-{int(datetime.now(UTC).timestamp() * 1000)}",
            chat_jid=chat_jid,
            sender="deploy",
            sender_name="deploy",
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
                    with contextlib.suppress(Exception):
                        await ch.send_message(chat_jid, text)
                else:
                    try:
                        await ch.send_message(chat_jid, text)
                    except Exception as exc:
                        logger.warning("Channel send failed", channel=ch.name, err=str(exc))

    async def _send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None:
        """Send a reaction emoji to a message on all channels that support it."""
        for ch in self.channels:
            if ch.is_connected() and hasattr(ch, "send_reaction"):
                await ch.send_reaction(chat_jid, message_id, sender, emoji)

    async def _set_typing_on_channels(self, chat_jid: str, is_typing: bool) -> None:
        """Set typing indicator on all channels that support it."""
        for ch in self.channels:
            if ch.is_connected() and hasattr(ch, "set_typing"):
                await ch.set_typing(chat_jid, is_typing)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _broadcast_host_message(self, chat_jid: str, text: str) -> None:
        """Store raw host message, send emoji-prefixed to channels."""
        ts = datetime.now(UTC).isoformat()
        await store_message_direct(
            id=f"host-{int(datetime.now(UTC).timestamp() * 1000)}",
            chat_jid=chat_jid,
            sender="host",
            sender_name="host",
            content=text,
            timestamp=ts,
            is_from_me=True,
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

    async def _send_clear_confirmation(self, chat_jid: str) -> None:
        """Set cleared_at, store and broadcast a system confirmation."""
        # Mark clear boundary — messages before this are hidden
        cleared_ts = datetime.now(UTC).isoformat()
        await set_chat_cleared_at(chat_jid, cleared_ts)
        self.event_bus.emit(ChatClearedEvent(chat_jid=chat_jid))

        await self._broadcast_host_message(chat_jid, "🗑️")

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
    # Service installation
    # ------------------------------------------------------------------

    @staticmethod
    def _is_launchd_managed() -> bool:
        """Check if this process was started by launchd (PPID 1)."""
        import os

        return os.getppid() == 1

    @staticmethod
    def _is_launchd_loaded(label: str) -> bool:
        """Check if a launchd job is loaded."""
        result = subprocess.run(["launchctl", "list", label], capture_output=True)
        return result.returncode == 0

    @staticmethod
    def _install_service() -> None:
        """Install the platform service file so the process auto-restarts on exit.

        On macOS: copies plist to ~/Library/LaunchAgents/ and loads it into
        launchd if we're already running under launchd (safe reload). When
        running manually, only copies the file to avoid spawning a competing
        second instance — the user runs launchctl load once to activate.
        """
        if sys.platform == "darwin":
            label = "com.pynchy"
            src = PROJECT_ROOT / "launchd" / f"{label}.plist"
            dest = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if not src.exists():
                logger.warning("launchd plist not found in repo, skipping service install")
                return
            already_loaded = PynchyApp._is_launchd_loaded(label)
            file_changed = not dest.exists() or not filecmp.cmp(str(src), str(dest), shallow=False)
            if not file_changed and already_loaded:
                return  # already up to date and loaded
            if file_changed:
                # Unload before overwriting so launchd picks up the new version
                if already_loaded:
                    subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                logger.info("Installed launchd plist", dest=str(dest))
            # Only load if we're already running under launchd (safe to reload).
            # When running manually, loading would spawn a competing instance
            # that fights over WhatsApp websocket and port binding.
            if already_loaded or PynchyApp._is_launchd_managed():
                subprocess.run(["launchctl", "load", str(dest)], capture_output=True)
                logger.info("Loaded launchd service", label=label)
            elif not already_loaded:
                logger.info(
                    "Launchd plist installed. To enable auto-restart, stop this "
                    "process and run: launchctl load ~/Library/LaunchAgents/com.pynchy.plist"
                )
        elif sys.platform == "linux":
            uv_path = shutil.which("uv")
            if not uv_path:
                logger.warning("uv not found in PATH, skipping systemd service install")
                return
            home = Path.home()
            # TODO: Uninstall cleanup — need a way to systemctl --user disable + rm
            # this service when the user wants to remove pynchy.
            unit = f"""\
[Unit]
Description=Pynchy personal assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={PROJECT_ROOT}
ExecStart={uv_path} run pynchy
Restart=always
RestartSec=10
Environment=HOME={home}
Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""
            dest = home / ".config" / "systemd" / "user" / "pynchy.service"
            if dest.exists() and dest.read_text() == unit:
                return  # already up to date
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(unit)
            logger.info("Installed systemd user service", dest=str(dest))
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
            )
            subprocess.run(
                ["systemctl", "--user", "enable", "pynchy.service"],
                capture_output=True,
            )
            # Enable lingering so the user service runs without an active login session
            subprocess.run(
                ["sudo", "loginctl", "enable-linger", home.name],
                capture_output=True,
            )

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

        # Hard-exit watchdog: if graceful shutdown hangs, force-exit after 12s.
        # This ensures launchd/systemd can restart us even if a container or
        # channel disconnect blocks indefinitely.
        import os

        loop = asyncio.get_running_loop()
        loop.call_later(12, lambda: os._exit(1))

        if self._http_runner:
            await self._http_runner.cleanup()
        await self.queue.shutdown(10.0)
        for channel in self.channels:
            await channel.disconnect()

    async def run(self) -> None:
        """Main entry point — startup sequence."""
        continuation_path = DATA_DIR / "deploy_continuation.json"

        try:
            self._install_service()
            self._ensure_container_system_running()
            await init_database()
            logger.info("Database initialized")
            await self._load_state()

            # Discover plugins after loading state
            from pynchy.plugin import discover_plugins

            self.registry = discover_plugins()
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

        # Create and connect plugin channels
        await self._connect_plugin_channels()

        # Reconcile periodic agents (create chat groups + tasks from periodic.yaml)
        await self._reconcile_periodic_agents()

        # Start subsystems
        asyncio.create_task(start_scheduler_loop(self._make_scheduler_deps()))
        asyncio.create_task(start_ipc_watcher(self._make_ipc_deps()))
        self.queue.set_process_messages_fn(self._process_group_messages)

        # HTTP server for remote health checks, deploys, and TUI API
        self._check_tailscale()
        self._http_runner = await start_http_server(self._make_http_deps())
        logger.info("HTTP server ready", port=DEPLOY_PORT)

        await self._send_boot_notification()
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
                # Need to format per channel, so can't use broadcast helper here
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
                await app._broadcast_to_channels(jid, text)

            async def broadcast_host_message(self, jid: str, text: str) -> None:
                await app._broadcast_host_message(jid, text)

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

            async def get_periodic_agents(self) -> list[dict[str, Any]]:
                from pynchy.periodic import load_periodic_config

                results = []
                for group in app.registered_groups.values():
                    config = load_periodic_config(group.folder)
                    if config is None:
                        continue
                    task = await get_active_task_for_group(group.folder)
                    results.append(
                        {
                            "name": group.name,
                            "folder": group.folder,
                            "schedule": config.schedule,
                            "context_mode": config.context_mode,
                            "last_run": task.last_run if task else None,
                            "next_run": task.next_run if task else None,
                            "status": task.status if task else "no_task",
                        }
                    )
                return results

            def subscribe_events(self, callback: Any) -> Any:
                from pynchy.event_bus import (
                    AgentActivityEvent,
                    AgentTraceEvent,
                    ChatClearedEvent,
                    MessageEvent,
                )

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

                async def on_trace(event: AgentTraceEvent) -> None:
                    await callback(
                        {
                            "type": "agent_trace",
                            "chat_jid": event.chat_jid,
                            "trace_type": event.trace_type,
                            **event.data,
                        }
                    )

                async def on_clear(event: ChatClearedEvent) -> None:
                    await callback(
                        {
                            "type": "chat_cleared",
                            "chat_jid": event.chat_jid,
                        }
                    )

                unsubs.append(app.event_bus.subscribe(MessageEvent, on_msg))
                unsubs.append(app.event_bus.subscribe(AgentActivityEvent, on_activity))
                unsubs.append(app.event_bus.subscribe(AgentTraceEvent, on_trace))
                unsubs.append(app.event_bus.subscribe(ChatClearedEvent, on_clear))

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
                await app._broadcast_to_channels(jid, text)

            async def broadcast_host_message(self, jid: str, text: str) -> None:
                await app._broadcast_host_message(jid, text)

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
                app._session_cleared.add(group_folder)
                await clear_session(group_folder)

            async def clear_chat_history(self, chat_jid: str) -> None:
                await app._send_clear_confirmation(chat_jid)

            def enqueue_message_check(self, group_jid: str) -> None:
                app.queue.enqueue_message_check(group_jid)

            def channels(self) -> list:
                return app.channels

        return _Deps()
