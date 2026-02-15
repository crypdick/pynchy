"""Pynchy Agent Runner — runs inside a container.

This is the framework-agnostic runner. It handles stdin/stdout framing, IPC
polling, and output marker wrapping. The actual LLM agent logic is delegated
to AgentCore implementations (Claude SDK, OpenAI, etc.).

Input protocol:
  Stdin: Full ContainerInput JSON (read until EOF)
  IPC:   Follow-up messages written as JSON files to /workspace/ipc/input/
         Sentinel: /workspace/ipc/input/_close — signals session end

Stdout protocol:
  Each result is wrapped in OUTPUT_START_MARKER / OUTPUT_END_MARKER pairs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from .core import AgentCoreConfig, AgentEvent
from .registry import create_agent_core

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
IPC_POLL_SECONDS = 0.5

OUTPUT_START_MARKER = "---PYNCHY_OUTPUT_START---"
OUTPUT_END_MARKER = "---PYNCHY_OUTPUT_END---"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ContainerInput:
    def __init__(self, data: dict[str, Any]) -> None:
        self.messages: list[dict[str, Any]] = data["messages"]
        self.session_id: str | None = data.get("session_id")
        self.group_folder: str = data["group_folder"]
        self.chat_jid: str = data["chat_jid"]
        self.is_god: bool = data["is_god"]
        self.is_scheduled_task: bool = data.get("is_scheduled_task", False)
        self.system_notices: list[str] | None = data.get("system_notices")
        self.project_access: bool = data.get("project_access", False)
        self.plugin_mcp_servers: dict[str, Any] | None = data.get("plugin_mcp_servers")
        self.agent_core_module: str = data.get("agent_core_module", "agent_runner.cores.claude")
        self.agent_core_class: str = data.get("agent_core_class", "ClaudeAgentCore")
        self.agent_core_config: dict[str, Any] | None = data.get("agent_core_config")


class ContainerOutput:
    def __init__(
        self,
        status: str,
        result: str | None = None,
        new_session_id: str | None = None,
        error: str | None = None,
        *,
        type: str = "result",
        thinking: str | None = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        text: str | None = None,
        system_subtype: str | None = None,
        system_data: dict[str, Any] | None = None,
        tool_result_id: str | None = None,
        tool_result_content: str | None = None,
        tool_result_is_error: bool | None = None,
        result_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.result = result
        self.new_session_id = new_session_id
        self.error = error
        self.type = type
        self.thinking = thinking
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.text = text
        self.system_subtype = system_subtype
        self.system_data = system_data
        self.tool_result_id = tool_result_id
        self.tool_result_content = tool_result_content
        self.tool_result_is_error = tool_result_is_error
        self.result_metadata = result_metadata

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "status": self.status}
        if self.type == "result":
            d["result"] = self.result
            if self.new_session_id:
                d["new_session_id"] = self.new_session_id
            if self.error:
                d["error"] = self.error
            if self.result_metadata:
                d["result_metadata"] = self.result_metadata
        elif self.type == "thinking":
            d["thinking"] = self.thinking
        elif self.type == "tool_use":
            d["tool_name"] = self.tool_name
            d["tool_input"] = self.tool_input
        elif self.type == "text":
            d["text"] = self.text
        elif self.type == "system":
            d["system_subtype"] = self.system_subtype
            d["system_data"] = self.system_data
        elif self.type == "tool_result":
            d["tool_result_id"] = self.tool_result_id
            d["tool_result_content"] = self.tool_result_content
            d["tool_result_is_error"] = self.tool_result_is_error
        return d


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_output(output: ContainerOutput) -> None:
    """Write a marker-wrapped output to stdout."""
    print(OUTPUT_START_MARKER)
    print(json.dumps(output.to_dict()))
    print(OUTPUT_END_MARKER)
    sys.stdout.flush()


def log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# IPC functions
# ---------------------------------------------------------------------------


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


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel.

    Returns the messages as a single string, or None if _close.
    """
    while True:
        if should_close():
            return None
        messages = drain_ipc_input()
        if messages:
            return "\n".join(messages)
        await asyncio.sleep(IPC_POLL_SECONDS)


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
    # Load global CLAUDE.md as additional system context (non-god groups only)
    global_claude_md_path = Path("/workspace/global/CLAUDE.md")
    system_prompt_append: str | None = None

    if not container_input.is_god and global_claude_md_path.exists():
        system_prompt_append = global_claude_md_path.read_text()

    # Append system notices to system prompt (SDK system messages FOR the LLM)
    # These provide context TO the LLM, distinct from operational host messages.
    # Examples: git health warnings, uncommitted changes, deployment state
    if container_input.system_notices:
        notices_text = "\n\n".join(container_input.system_notices)
        if system_prompt_append:
            system_prompt_append += "\n\n" + notices_text
        else:
            system_prompt_append = notices_text

    # MCP server path for IPC tools
    mcp_server_command = "python"
    mcp_server_args = ["-m", "agent_runner.ipc_mcp"]

    # Build mcp_servers dict starting with built-in pynchy server
    mcp_servers_dict = {
        "pynchy": {
            "command": mcp_server_command,
            "args": mcp_server_args,
            "env": {
                "PYNCHY_CHAT_JID": container_input.chat_jid,
                "PYNCHY_GROUP_FOLDER": container_input.group_folder,
                "PYNCHY_IS_GOD": ("1" if container_input.is_god else "0"),
                "PYNCHY_SESSION_ID": (container_input.session_id or ""),
                "PYNCHY_IS_SCHEDULED_TASK": ("1" if container_input.is_scheduled_task else "0"),
            },
        },
    }

    # Merge plugin MCP servers
    if container_input.plugin_mcp_servers:
        for name, spec in container_input.plugin_mcp_servers.items():
            plugin_env = spec.get("env", {}).copy()
            # Add PYTHONPATH for plugin source imports
            plugin_env["PYTHONPATH"] = f"/workspace/plugins/{name}"
            mcp_servers_dict[name] = {
                "command": spec["command"],
                "args": spec["args"],
                "env": plugin_env,
            }

    # Default cwd to the mounted project repo when available, so agents start
    # in the codebase they're working on rather than the group metadata dir.
    # God always has /workspace/project; non-god gets it via project_access.
    has_project = container_input.is_god or container_input.project_access
    agent_cwd = "/workspace/project" if has_project else "/workspace/group"

    # Build extra config from agent_core_config
    extra = container_input.agent_core_config or {}

    return AgentCoreConfig(
        cwd=agent_cwd,
        session_id=container_input.session_id,
        group_folder=container_input.group_folder,
        chat_jid=container_input.chat_jid,
        is_god=container_input.is_god,
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
            return ContainerOutput(
                status="success",
                result=event.data.get("result"),
                new_session_id=session_id,
                result_metadata=event.data.get("result_metadata"),
            )
        case _:
            log(f"Unknown event type: {event.type}")
            return ContainerOutput(status="success", type="text", text="")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # Read input from stdin
    try:
        stdin_data = sys.stdin.read()
        container_input = ContainerInput(json.loads(stdin_data))
        log(f"Received input for group: {container_input.group_folder}")
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        log(f"Using agent core: {core_ref}")
    except Exception as exc:
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to parse input: {exc}",
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
