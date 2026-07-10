"""Pynchy Agent Runner — runs inside a container.

This is the framework-agnostic runner. It handles message conversion, core
configuration, and the agent query loop. The actual LLM agent logic is
delegated to AgentCore implementations (Claude SDK, OpenAI, etc.).

IPC protocol details (file-based input/output) live in ``ipc.py``.
"""

from __future__ import annotations

import contextlib
import json
import sys

from .core import AgentCore, AgentCoreConfig, AgentEvent
from .ipc import (
    IPC_INPUT_CLOSE_SENTINEL,
    IPC_INPUT_DIR,
    drain_ipc_input,
    log,
    read_initial_input,
    should_close,
    wait_for_ipc_message,
    write_output,
)
from .models import ContainerInput, ContainerOutput
from .registry import create_agent_core

# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def build_sdk_messages(messages: list[dict[str, object]]) -> str:
    """Convert message list to the format the SDK's query() method expects.

    Wraps each message in a ``<message>`` XML element.

    Message types:
    - 'user': From humans
    - 'assistant': Responses from the LLM
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
        metadata = msg.get("metadata")
        if metadata is not None:
            metadata_json = escape_xml(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
            content = (
                f"{content}\n<metadata>{metadata_json}</metadata>"
                if content
                else f"<metadata>{metadata_json}</metadata>"
            )
        lines.append(f'<message sender="{sender_name}" time="{timestamp}">{content}</message>')

    return f"<messages>\n{chr(10).join(lines)}\n</messages>"


# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------


def _built_in_mcp_server(container_input: ContainerInput) -> dict[str, object]:
    """Build the always-present Pynchy MCP server entry."""
    return {
        "command": "python",
        "args": ["-m", "agent_runner.agent_tools"],
        "env": {
            "PYNCHY_CHAT_JID": container_input.chat_jid,
            "PYNCHY_GROUP_FOLDER": container_input.group_folder,
            "PYNCHY_IS_ADMIN": ("1" if container_input.is_admin else "0"),
            "PYNCHY_SESSION_ID": (container_input.session_id or ""),
            "PYNCHY_IS_SCHEDULED_TASK": ("1" if container_input.is_scheduled_task else "0"),
        },
    }


def _direct_mcp_server_entry(server: dict[str, object]) -> dict[str, object]:
    """Normalize a direct MCP server config for the agent core."""
    transport = server.get("transport", "sse")
    url = server["url"]
    match transport:
        case "sse":
            normalized_url = f"{url}/sse"
            normalized_transport = "sse"
        case "http" | "streamable_http":
            normalized_url = f"{url}/mcp"
            normalized_transport = "http"
        case _:
            normalized_url = url
            normalized_transport = transport

    return {
        "type": normalized_transport,
        "url": normalized_url,
    }


def _build_mcp_servers(container_input: ContainerInput) -> dict[str, dict[str, object]]:
    """Build the MCP server map for the selected container input."""
    mcp_servers: dict[str, dict[str, object]] = {"pynchy": _built_in_mcp_server(container_input)}
    direct_servers = container_input.mcp_direct_servers
    if not direct_servers:
        return mcp_servers

    # Add remote MCP servers — connect directly to containers, bypassing
    # LiteLLM's MCP proxy (which doesn't work with Claude SDK; see
    # backlog/3-ready/mcp-gateway-transport.md).
    log(f"Direct MCP servers received: {direct_servers}")
    for server in direct_servers:
        entry = _direct_mcp_server_entry(server)
        log(f"Configuring MCP server '{server['name']}': {entry}")
        mcp_servers[server["name"]] = entry

    return mcp_servers


def _agent_cwd(container_input: ContainerInput) -> str:
    """Pick the container cwd based on mounted repo availability."""
    primary_repo = (container_input.repo_accesses or [container_input.repo_access or ""])[0]
    if primary_repo:
        owner, repo_name = primary_repo.split("/", 1)
        return f"/workspace/repos/{owner}/{repo_name}"
    return "/workspace/group"


def build_core_config(container_input: ContainerInput) -> AgentCoreConfig:
    """Build AgentCoreConfig from ContainerInput."""
    # Directives are resolved host-side and passed in via system_prompt_append.
    system_prompt_append = container_input.system_prompt_append

    # IMPORTANT: Do NOT append ephemeral per-run content (system notices, dirty
    # worktree warnings, etc.) to the system prompt. Changing the system prompt
    # between session resumes invalidates the entire KV cache, forcing the API
    # to reprocess the full conversation history — expensive in both tokens and
    # latency. System notices are prepended to the user prompt in main() instead.

    # Build extra config from agent_core_config
    extra = container_input.agent_core_config or {}

    return AgentCoreConfig(
        cwd=_agent_cwd(container_input),
        session_id=container_input.session_id,
        group_folder=container_input.group_folder,
        chat_jid=container_input.chat_jid,
        is_admin=container_input.is_admin,
        is_scheduled_task=container_input.is_scheduled_task,
        system_prompt_append=system_prompt_append,
        mcp_servers=_build_mcp_servers(container_input),
        # No plugin hooks are configured yet. Enforcement is fully wired: every
        # core (incl. CLI PreToolUse subprocesses) composes its gate via
        # before_tool_use_roster, so the moment this list is populated all cores
        # enforce it identically. TODO: source real specs from container_input.
        plugin_hooks=[],
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Event conversion
# ---------------------------------------------------------------------------


def _success_output(output_type: str, **kwargs: object) -> ContainerOutput:
    """Build a successful non-result container output."""
    return ContainerOutput(status="success", type=output_type, **kwargs)


def _result_output(event: AgentEvent, session_id: str | None) -> ContainerOutput:
    """Build the terminal result output, including error propagation."""
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


def event_to_output(event: AgentEvent, session_id: str | None) -> ContainerOutput:
    """Convert AgentEvent to ContainerOutput."""
    match event.type:
        case "thinking":
            output = _success_output("thinking", thinking=event.data.get("thinking"))
        case "tool_use":
            output = _success_output(
                "tool_use",
                tool_name=event.data.get("tool_name"),
                tool_input=event.data.get("tool_input"),
            )
        case "tool_result":
            output = _success_output(
                "tool_result",
                tool_result_id=event.data.get("tool_result_id"),
                tool_result_content=event.data.get("tool_result_content"),
                tool_result_is_error=event.data.get("tool_result_is_error"),
            )
        case "text":
            output = _success_output("text", text=event.data.get("text"))
        case "system":
            output = _success_output(
                "system",
                system_subtype=event.data.get("system_subtype"),
                system_data=event.data.get("system_data", {}),
            )
        case "result":
            output = _result_output(event, session_id)
        case _:
            log(f"Unknown event type: {event.type}")
            output = _success_output("text", text="")
    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _read_container_input() -> ContainerInput:
    """Read initial input from file (written by host before container start)."""
    try:
        container_input = read_initial_input()
    except Exception as exc:  # allow: exception-handling; report to host  # noqa: BLE001, RUF100
        write_output(ContainerOutput(status="error", error=f"Failed to read initial input: {exc}"))
        sys.exit(1)
    else:
        log(f"Received input for group: {container_input.group_folder}")
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        log(f"Using agent core: {core_ref}")
        return container_input


def _build_initial_prompt(container_input: ContainerInput) -> str:
    """Build the initial prompt: SDK messages, scheduled-task framing, notices, pending IPC."""
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

    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    return prompt


async def _create_and_start_core(container_input: ContainerInput) -> AgentCore:
    """Create and start the agent core, exiting the process on failure."""
    core_config = build_core_config(container_input)

    try:
        core = create_agent_core(
            container_input.agent_core_module, container_input.agent_core_class, core_config
        )
    except Exception as exc:  # allow: exception-handling; report to host  # noqa: BLE001, RUF100
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        write_output(
            ContainerOutput(
                status="error", error=f"Failed to create agent core '{core_ref}': {exc}"
            )
        )
        sys.exit(1)

    try:
        await core.start()
    except Exception as exc:  # allow: exception-handling; report to host  # noqa: BLE001, RUF100
        write_output(ContainerOutput(status="error", error=f"Failed to start agent core: {exc}"))
        sys.exit(1)

    return core


async def _run_single_query(
    core: AgentCore, prompt: str, session_id: str | None
) -> tuple[str | None, int, bool]:
    """Run one query to completion, streaming events to the host.

    Returns (new_session_id, result_count, closed_during_query).
    """
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

    return new_session_id, result_count, closed_during_query


async def _drive_conversation_loop(core: AgentCore, prompt: str, session_id: str | None) -> None:
    """Run the conversation loop until the host closes the IPC channel."""
    while True:
        new_session_id, result_count, closed_during_query = await _run_single_query(
            core, prompt, session_id
        )

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
        write_output(ContainerOutput(status="success", result=None, new_session_id=session_id))

        log("Query ended, waiting for next IPC message...")

        next_message = await wait_for_ipc_message()
        if next_message is None:
            log("Close sentinel received, exiting")
            break

        log(f"Got new message ({len(next_message)} chars), starting new query")
        prompt = next_message


async def _run_conversation_loop(core: AgentCore, prompt: str, session_id: str | None) -> None:
    """Drive query → wait-for-next-message cycles until the host signals close."""
    try:
        await _drive_conversation_loop(core, prompt, session_id)

    except Exception as exc:  # allow: exception-handling; loop  # noqa: BLE001, RUF100
        error_message = str(exc)
        log(f"Agent error: {error_message}")
        write_output(
            ContainerOutput(status="error", new_session_id=session_id, error=error_message)
        )
        sys.exit(1)
    finally:
        try:
            await core.stop()
        except Exception as exc:  # allow: exception-handling; cleanup  # noqa: BLE001, RUF100
            log(f"Error stopping core: {exc}")


async def main() -> None:
    container_input = _read_container_input()

    # Clean up stale _close sentinel
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        IPC_INPUT_CLOSE_SENTINEL.unlink()

    prompt = _build_initial_prompt(container_input)
    core = await _create_and_start_core(container_input)
    await _run_conversation_loop(core, prompt, container_input.session_id)
