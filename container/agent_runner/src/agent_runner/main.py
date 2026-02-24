"""Pynchy Agent Runner — runs inside a container.

This is the framework-agnostic runner. It handles initial input parsing, IPC
polling, and output file writing. The actual LLM agent logic is delegated
to AgentCore implementations (Claude SDK, OpenAI, etc.).

Input protocol:
  Initial: ContainerInput JSON read from /workspace/ipc/input/initial.json
           (written by host before container start, deleted after read)
  IPC:     Follow-up messages written as JSON files to /workspace/ipc/input/
           Sentinel: /workspace/ipc/input/_close — signals session end

Output protocol:
  Each event is written as a JSON file to /workspace/ipc/output/.
  Filenames are monotonic nanosecond timestamps ({ns}.json) for guaranteed
  ordering. Files are written atomically (write .json.tmp, then rename).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .core import AgentCoreConfig, AgentEvent
from .models import ContainerInput, ContainerOutput
from .registry import create_agent_core

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
INITIAL_INPUT_FILE = IPC_INPUT_DIR / "initial.json"

IPC_OUTPUT_DIR = Path("/workspace/ipc/output")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_output(output: ContainerOutput) -> None:
    """Write an output event as a JSON file to the IPC output directory.

    Uses monotonic_ns timestamps for filenames to guarantee ordering.
    Writes atomically: data goes to a .json.tmp file first, then is
    renamed to .json so the host-side watcher never sees partial writes.
    """
    IPC_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{time.monotonic_ns()}.json"
    final_path = IPC_OUTPUT_DIR / filename
    tmp_path = final_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(output.to_dict()))
    tmp_path.rename(final_path)


def log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# IPC functions
# ---------------------------------------------------------------------------


def read_initial_input() -> ContainerInput:
    """Read initial ContainerInput from the IPC input file.

    The host writes ``initial.json`` to the IPC input directory before
    starting the container.  We read it once on startup, parse it into a
    ``ContainerInput``, and delete the file so ``drain_ipc_input()`` never
    picks it up as a follow-up message.

    Raises ``FileNotFoundError`` if the file is missing (container was
    started without the host writing initial input).
    """
    data = json.loads(INITIAL_INPUT_FILE.read_text())
    container_input = ContainerInput.from_dict(data)
    INITIAL_INPUT_FILE.unlink()
    return container_input


def should_close() -> bool:
    """Check for _close sentinel."""
    if IPC_INPUT_CLOSE_SENTINEL.exists():
        with contextlib.suppress(OSError):
            IPC_INPUT_CLOSE_SENTINEL.unlink()
        return True
    return False


def drain_ipc_input() -> list[str]:
    """Drain all pending IPC input messages. Returns messages found."""
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")

        messages: list[str] = []
        for file_path in files:
            try:
                data = json.loads(file_path.read_text())
                file_path.unlink()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except Exception as exc:
                log(f"Failed to process input file {file_path.name}: {exc}")
                with contextlib.suppress(OSError):
                    file_path.unlink()
        return messages
    except Exception as exc:
        log(f"IPC drain error: {exc}")
        return []


class _InputEventHandler(FileSystemEventHandler):
    """Watchdog handler that signals an asyncio.Event when input files appear.

    Runs in the watchdog background thread; uses call_soon_threadsafe to wake
    the async event loop.  Matches the pattern used by the host-side watcher
    (src/pynchy/ipc/_watcher.py).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        super().__init__()
        self._loop = loop
        self._event = event

    def _signal_if_relevant(self, path_str: str) -> None:
        p = Path(path_str)
        # Wake up for .json message files or the _close sentinel
        if p.suffix == ".json" or p.name == "_close":
            self._loop.call_soon_threadsafe(self._event.set)

    def on_created(self, event: Any) -> None:
        if isinstance(event, FileCreatedEvent):
            self._signal_if_relevant(event.src_path)

    def on_moved(self, event: Any) -> None:
        # Host writes atomically (tmp -> rename), which produces a moved event
        if isinstance(event, FileMovedEvent):
            self._signal_if_relevant(event.dest_path)


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel.

    Uses watchdog to detect new files in IPC_INPUT_DIR instead of polling.
    Returns the messages as a single string, or None if _close.
    """
    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()

    handler = _InputEventHandler(loop, wakeup)
    observer = Observer()
    observer.schedule(handler, str(IPC_INPUT_DIR), recursive=False)
    observer.daemon = True
    observer.start()

    try:
        while True:
            if should_close():
                return None
            messages = drain_ipc_input()
            if messages:
                return "\n".join(messages)
            # Wait until watchdog signals new file activity, then re-check
            await wakeup.wait()
            wakeup.clear()
    finally:
        observer.stop()
        observer.join(timeout=2)


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def build_sdk_messages(messages: list[dict[str, Any]]) -> str:
    """Convert message list to SDK-compatible format.

    For now, we convert to XML format for compatibility. In the future,
    this will build proper SDK message objects (UserMessage, AssistantMessage, etc.)
    once the SDK supports that in the query() method.

    Message types:
    - 'user': From humans
    - 'assistant': From LLM (previous responses)
    - 'system': Context for LLM (currently handled via system_prompt)
    - 'tool_result': Command outputs, tool execution results
    - 'host': Operational notifications (FILTERED OUT - should never reach here)
    """
    if not messages:
        return ""

    def escape_xml(s: str) -> str:
        """Escape XML special characters."""
        return (
            s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        )

    lines = []
    for msg in messages:
        sender_name = escape_xml(msg.get("sender_name", "Unknown"))
        timestamp = msg.get("timestamp", "")
        content = escape_xml(msg.get("content", ""))
        lines.append(f'<message sender="{sender_name}" time="{timestamp}">{content}</message>')

    return f"<messages>\n{chr(10).join(lines)}\n</messages>"


# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------


def build_core_config(container_input: ContainerInput) -> AgentCoreConfig:
    """Build AgentCoreConfig from ContainerInput."""
    # Directives are resolved host-side and passed in via system_prompt_append.
    # This replaced the old global/CLAUDE.md file-reading approach.
    system_prompt_append = container_input.system_prompt_append

    # IMPORTANT: Do NOT append ephemeral per-run content (system notices, dirty
    # worktree warnings, etc.) to the system prompt. Changing the system prompt
    # between session resumes invalidates the entire KV cache, forcing the API
    # to reprocess the full conversation history — expensive in both tokens and
    # latency. System notices are prepended to the user prompt in main() instead.

    # MCP server path for agent tools
    mcp_server_command = "python"
    mcp_server_args = ["-m", "agent_runner.agent_tools"]

    # Build mcp_servers dict starting with built-in pynchy server
    mcp_servers_dict = {
        "pynchy": {
            "command": mcp_server_command,
            "args": mcp_server_args,
            "env": {
                "PYNCHY_CHAT_JID": container_input.chat_jid,
                "PYNCHY_GROUP_FOLDER": container_input.group_folder,
                "PYNCHY_IS_ADMIN": ("1" if container_input.is_admin else "0"),
                "PYNCHY_SESSION_ID": (container_input.session_id or ""),
                "PYNCHY_IS_SCHEDULED_TASK": ("1" if container_input.is_scheduled_task else "0"),
            },
        },
    }

    # Add remote MCP servers — connect directly to containers, bypassing
    # LiteLLM's MCP proxy (which doesn't work with Claude SDK; see
    # backlog/3-ready/mcp-gateway-transport.md).
    if container_input.mcp_direct_servers:
        log(f"Direct MCP servers received: {container_input.mcp_direct_servers}")
        for server in container_input.mcp_direct_servers:
            transport = server.get("transport", "sse")
            url = server["url"]
            # SSE servers expose /sse endpoint; streamable HTTP uses /mcp
            if transport == "sse":
                url = f"{url}/sse"
            elif transport in ("http", "streamable_http"):
                url = f"{url}/mcp"
            entry = {
                "type": transport if transport != "streamable_http" else "http",
                "url": url,
            }
            log(f"Configuring MCP server '{server['name']}': {entry}")
            mcp_servers_dict[server["name"]] = entry

    # Default cwd to the mounted project repo when available, so agents start
    # in the codebase they're working on rather than the group metadata dir.
    # Admin always has /workspace/project; non-admin gets it via repo_access.
    has_repo_mount = container_input.is_admin or bool(container_input.repo_access)
    agent_cwd = "/workspace/project" if has_repo_mount else "/workspace/group"

    # Build extra config from agent_core_config
    extra = container_input.agent_core_config or {}

    return AgentCoreConfig(
        cwd=agent_cwd,
        session_id=container_input.session_id,
        group_folder=container_input.group_folder,
        chat_jid=container_input.chat_jid,
        is_admin=container_input.is_admin,
        is_scheduled_task=container_input.is_scheduled_task,
        system_prompt_append=system_prompt_append,
        mcp_servers=mcp_servers_dict,
        plugin_hooks=[],  # TODO: load plugin hooks from container_input
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Event conversion
# ---------------------------------------------------------------------------


def event_to_output(event: AgentEvent, session_id: str | None) -> ContainerOutput:
    """Convert AgentEvent to ContainerOutput."""
    match event.type:
        case "thinking":
            return ContainerOutput(
                status="success",
                type="thinking",
                thinking=event.data.get("thinking"),
            )
        case "tool_use":
            return ContainerOutput(
                status="success",
                type="tool_use",
                tool_name=event.data.get("tool_name"),
                tool_input=event.data.get("tool_input"),
            )
        case "tool_result":
            return ContainerOutput(
                status="success",
                type="tool_result",
                tool_result_id=event.data.get("tool_result_id"),
                tool_result_content=event.data.get("tool_result_content"),
                tool_result_is_error=event.data.get("tool_result_is_error"),
            )
        case "text":
            return ContainerOutput(
                status="success",
                type="text",
                text=event.data.get("text"),
            )
        case "system":
            return ContainerOutput(
                status="success",
                type="system",
                system_subtype=event.data.get("system_subtype"),
                system_data=event.data.get("system_data", {}),
            )
        case "result":
            meta = event.data.get("result_metadata") or {}
            is_error = meta.get("is_error", False)
            result_text = event.data.get("result")
            return ContainerOutput(
                status="error" if is_error else "success",
                result=result_text,
                new_session_id=session_id,
                error=result_text if is_error else None,
                result_metadata=meta or None,
            )
        case _:
            log(f"Unknown event type: {event.type}")
            return ContainerOutput(status="success", type="text", text="")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # Read initial input from file (written by host before container start)
    try:
        container_input = read_initial_input()
        log(f"Received input for group: {container_input.group_folder}")
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        log(f"Using agent core: {core_ref}")
    except Exception as exc:
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to read initial input: {exc}",
            )
        )
        sys.exit(1)

    # Clean up stale _close sentinel
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        IPC_INPUT_CLOSE_SENTINEL.unlink()

    # Build initial prompt from SDK messages
    log(f"Using SDK message list ({len(container_input.messages)} messages)")
    prompt = build_sdk_messages(container_input.messages)

    if container_input.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK]\n"
            "This is an automated scheduled task — not a live user conversation. "
            "Your container will be destroyed when you finish.\n\n"
            "Lifecycle:\n"
            "1. Complete the work described below\n"
            "2. Commit and call sync_worktree_to_main (if you have project access)\n"
            "3. Call finished_work() to shut down cleanly\n\n"
            "Calling finished_work() merges any un-synced commits (safety net) "
            "and terminates this container. Do NOT continue work after calling it.\n\n" + prompt
        )

    # Prepend system notices as part of the user message rather than the system
    # prompt. This is ephemeral per-run context (dirty worktree, unpushed commits)
    # that must NOT go in the system prompt — see build_core_config() comment.
    if container_input.system_notices:
        notices_text = "\n".join(
            f"[System Notice] {notice}" for notice in container_input.system_notices
        )
        prompt = notices_text + "\n\n" + prompt

    # Drain any pending IPC messages into initial prompt
    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Build core config
    core_config = build_core_config(container_input)

    # Create and start agent core
    try:
        core = create_agent_core(
            container_input.agent_core_module, container_input.agent_core_class, core_config
        )
    except Exception as exc:
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to create agent core '{core_ref}': {exc}",
            )
        )
        sys.exit(1)

    try:
        await core.start()
    except Exception as exc:
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to start agent core: {exc}",
            )
        )
        sys.exit(1)

    session_id = container_input.session_id

    try:
        while True:
            log(f"Starting query (session: {session_id or 'new'})...")

            result_count = 0
            closed_during_query = False
            new_session_id: str | None = None

            async for event in core.query(prompt):
                # Check for close during query
                if should_close():
                    log("Close sentinel detected during query")
                    closed_during_query = True
                    break

                # Track session ID from system init events
                if event.type == "system":
                    subtype = event.data.get("system_subtype")
                    if subtype == "init":
                        sid = event.data.get("system_data", {}).get("session_id")
                        if sid:
                            new_session_id = sid
                            log(f"Session initialized: {new_session_id}")

                # Track results
                if event.type == "result":
                    result_count += 1

                # Convert event to output and write
                output = event_to_output(event, new_session_id or session_id)
                write_output(output)

            # Update session ID from core after query
            if core.session_id:
                session_id = core.session_id
            elif new_session_id:
                session_id = new_session_id

            log(f"Query done. Results: {result_count}, closedDuringQuery: {closed_during_query}")

            # If _close was consumed during the query, exit immediately
            if closed_during_query:
                log("Close sentinel consumed during query, exiting")
                break

            # Emit session update so host can track it
            write_output(
                ContainerOutput(
                    status="success",
                    result=None,
                    new_session_id=session_id,
                )
            )

            log("Query ended, waiting for next IPC message...")

            next_message = await wait_for_ipc_message()
            if next_message is None:
                log("Close sentinel received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message

    except Exception as exc:
        error_message = str(exc)
        log(f"Agent error: {error_message}")
        write_output(
            ContainerOutput(
                status="error",
                new_session_id=session_id,
                error=error_message,
            )
        )
        sys.exit(1)
    finally:
        # Clean up core
        try:
            await core.stop()
        except Exception as exc:
            log(f"Error stopping core: {exc}")
