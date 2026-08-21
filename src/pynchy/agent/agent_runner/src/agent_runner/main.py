"""Pynchy Agent Runner — runs inside a container.

This is the framework-agnostic runner. It handles message conversion, core
configuration, and the agent query loop. The actual LLM agent logic is
delegated to AgentCore implementations (Claude SDK, OpenAI, etc.).

IPC protocol details (file-based input/output) live in ``ipc.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, TypedDict, Unpack

from .core import AgentCore, AgentCoreConfig
from .events import (
    AgentEvent,
    ResultEvent,
    SystemEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    validate_agent_stream,
)
from .ipc import (
    IPC_INPUT_CLOSE_SENTINEL,
    IPC_INPUT_DIR,
    drain_ipc_input,
    log,
    read_initial_input,
    should_close,
    wait_for_ipc_followup,
    write_output,
)
from .models import ContainerInput, ContainerOutput
from .paths import AGENT_SOURCE_ROOT, AGENT_WORKSPACE
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
        sender = msg.get("sender_name", "Unknown")
        sender_name = escape_xml(sender if isinstance(sender, str) else "Unknown")
        timestamp = msg.get("timestamp", "")
        raw_content = msg.get("content", "")
        content = escape_xml(raw_content if isinstance(raw_content, str) else "")
        context = msg.get("context")
        if context is not None:
            context_json = escape_xml(json.dumps(context, ensure_ascii=False, sort_keys=True))
            content = (
                f"{content}\n<context>{context_json}</context>"
                if content
                else f"<context>{context_json}</context>"
            )
        lines.append(f'<message sender="{sender_name}" time="{timestamp}">{content}</message>')

    return f"<messages>\n{chr(10).join(lines)}\n</messages>"


# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------


def _built_in_mcp_server(container_input: ContainerInput) -> dict[str, object]:
    """Build the always-present Pynchy MCP server entry."""
    env = {
        "PYNCHY_CHAT_JID": container_input.chat_jid,
        "PYNCHY_GROUP_FOLDER": container_input.group_folder,
        "PYNCHY_IS_ADMIN": ("1" if container_input.is_admin else "0"),
        "PYNCHY_SESSION_ID": (container_input.session_id or ""),
        "PYNCHY_IS_SCHEDULED_TASK": ("1" if container_input.is_scheduled_task else "0"),
    }
    for name in ("PYNCHY_IPC_DIR", "PYNCHY_SKILLS_ROOT"):
        if value := os.environ.get(name):
            env[name] = value
    result: dict[str, object] = {
        "command": "python",
        "args": ["-m", "agent_runner.agent_tools"],
        "env": env,
    }
    if (
        container_input.agent_core_module == "agent_runner.cores.codex"
        and container_input.agent_tool_grants is not None
    ):
        from agent_runner.agent_tools import enabled_agent_tools  # noqa: PLC0415

        result["enabled_tools"] = enabled_agent_tools(container_input.agent_tool_grants)
        result["required"] = True
    return result


def _direct_mcp_server_entry(server: dict[str, object]) -> dict[str, object]:
    """Normalize a direct MCP server config for the agent core."""
    transport = server.get("transport", "sse")
    url = server.get("url")
    if not isinstance(transport, str):
        raise TypeError("Direct MCP server transport must be a string")
    if not isinstance(url, str):
        raise TypeError("Direct MCP server URL must be a string")
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

    # Add remote MCP servers — connect directly to containers because LiteLLM's
    # MCP proxy does not support the Claude SDK.
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
        return str(AGENT_SOURCE_ROOT / owner / repo_name)
    return str(AGENT_WORKSPACE)


def _turn_metadata(turn_id: str, chat_jid: str, group_folder: str) -> dict[str, str]:
    return {
        "pynchy_turn_id": turn_id,
        "pynchy_chat_jid": chat_jid,
        "pynchy_group_folder": group_folder,
    }


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
    extra = dict(container_input.agent_core_config or {})
    if container_input.query_id is not None:
        extra["pynchy_query_id"] = container_input.query_id
    if container_input.turn_id:
        metadata = dict(extra.get("metadata") or {})
        metadata.update(
            _turn_metadata(
                container_input.turn_id,
                container_input.chat_jid,
                container_input.group_folder,
            )
        )
        extra["metadata"] = metadata

    return AgentCoreConfig(
        cwd=_agent_cwd(container_input),
        session_id=container_input.session_id,
        group_folder=container_input.group_folder,
        chat_jid=container_input.chat_jid,
        turn_id=container_input.turn_id,
        is_admin=container_input.is_admin,
        is_scheduled_task=container_input.is_scheduled_task,
        system_prompt_append=system_prompt_append,
        mcp_servers=_build_mcp_servers(container_input),
        plugin_hooks=container_input.plugin_hooks,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Event conversion
# ---------------------------------------------------------------------------


class _SuccessOutputFields(TypedDict, total=False):
    thinking: str | None
    tool_name: str | None
    tool_input: dict[str, Any] | None
    text: str | None
    system_subtype: str | None
    system_data: dict[str, Any] | None
    tool_result_id: str | None
    tool_result_content: str | None
    tool_result_is_error: bool | None


def _success_output(output_type: str, **kwargs: Unpack[_SuccessOutputFields]) -> ContainerOutput:
    """Build a successful non-result container output."""
    return ContainerOutput(status="success", type=output_type, **kwargs)


def _result_output(event: ResultEvent, session_id: str | None) -> ContainerOutput:
    """Build the terminal result output, including error propagation."""
    meta = event.result_metadata.to_dict()
    is_error = event.result_metadata.is_error
    result_text = event.result
    return ContainerOutput(
        status="error" if is_error else "success",
        result=result_text,
        new_session_id=session_id,
        error=result_text if is_error else None,
        result_metadata=meta or None,
    )


def event_to_output(event: AgentEvent, session_id: str | None) -> ContainerOutput:
    """Convert AgentEvent to ContainerOutput."""
    match event:
        case ThinkingEvent():
            output = _success_output("thinking", thinking=event.thinking)
        case ToolUseEvent():
            output = _success_output(
                "tool_use",
                tool_name=event.tool_name,
                tool_input=dict(event.tool_input),
            )
        case ToolResultEvent():
            output = _success_output(
                "tool_result",
                tool_result_id=event.tool_result_id,
                tool_result_content=event.tool_result_content,
                tool_result_is_error=event.tool_result_is_error,
            )
        case TextEvent():
            output = _success_output("text", text=event.text)
        case SystemEvent():
            output = _success_output(
                "system",
                system_subtype=event.system_subtype,
                system_data=dict(event.system_data),
            )
        case ResultEvent():
            output = _result_output(event, session_id)
    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _read_container_input() -> ContainerInput:
    """Read initial input from file (written by host before container start)."""
    try:
        container_input = read_initial_input()
    except Exception as exc:  # allow: exception-handling; report to host  # noqa: BLE001
        write_output(ContainerOutput(status="error", error=f"Failed to read initial input: {exc}"))
        sys.exit(1)
    else:
        log(f"Received input for group: {container_input.group_folder}")
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        log(f"Using agent core: {core_ref}")
        return container_input


def build_agent_prompt(container_input: ContainerInput) -> str:
    """Build the agent prompt from messages, scheduled-task framing, and notices."""
    prompt = build_sdk_messages(container_input.messages)

    if container_input.is_scheduled_task:
        # The host supplies a durable workspace and capabilities. The agent owns
        # the workflow because prescribed lifecycle steps make capable models
        # less adaptable and prevent them from recovering from tool failures.
        # A scheduled run has no live user to notice a recurring snag, so it
        # surfaces only actionable problems that it could not resolve itself.
        prompt = (
            "[SCHEDULED TASK]\n"
            "This is an automated scheduled task — not a live user conversation. "
            "Complete the requested objective within the authority granted by your tools "
            "and workspace policy. Report the outcome and relevant evidence in your final "
            "response. Fix ordinary snags yourself when possible and continue the scheduled "
            "objective rather than giving up at the problem.\n\n" + prompt
        )

    # Prepend system notices as part of the user message rather than the system
    # prompt. This is ephemeral per-run context (dirty worktree, unpushed commits)
    # that must NOT go in the system prompt — see build_core_config() comment.
    if container_input.system_notices:
        notices_text = "\n".join(
            f"[System Notice] {notice}" for notice in container_input.system_notices
        )
        prompt = notices_text + "\n\n" + prompt

    return prompt


def build_initial_prompt(container_input: ContainerInput) -> str:
    """Build the initial prompt, including pending container IPC messages."""
    log(f"Using SDK message list ({len(container_input.messages)} messages)")
    prompt = build_agent_prompt(container_input)

    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    return prompt


async def _create_and_start_core(
    container_input: ContainerInput,
) -> tuple[AgentCore, AgentCoreConfig]:
    """Create and start the agent core, exiting the process on failure."""
    core_config = build_core_config(container_input)

    try:
        core = create_agent_core(
            container_input.agent_core_module, container_input.agent_core_class, core_config
        )
    except Exception as exc:  # allow: exception-handling; report to host  # noqa: BLE001
        core_ref = f"{container_input.agent_core_module}.{container_input.agent_core_class}"
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to create agent core '{core_ref}': {exc}",
                query_id=container_input.query_id,
            )
        )
        sys.exit(1)

    try:
        await core.start()
    except Exception as exc:  # allow: exception-handling; report to host  # noqa: BLE001
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to start agent core: {exc}",
                query_id=container_input.query_id,
            )
        )
        sys.exit(1)

    return core, core_config


async def _run_single_query(
    core: AgentCore,
    prompt: str,
    session_id: str | None,
    *,
    query_id: str | None,
) -> tuple[str | None, int, bool]:
    """Run one query to completion, streaming events to the host.

    Returns (new_session_id, result_count, closed_during_query).
    """
    log(f"Starting query (session: {session_id or 'new'})...")

    result_count = 0
    closed_during_query = False
    new_session_id: str | None = None

    async for event in validate_agent_stream(core.query(prompt)):
        # Check for close during query
        if should_close():
            log("Close sentinel detected during query")
            closed_during_query = True
            break

        # Track session ID from system init events
        if isinstance(event, SystemEvent) and event.system_subtype == "init":
            sid = event.system_data.get("session_id")
            if isinstance(sid, str) and sid:
                new_session_id = sid
                log(f"Session initialized: {new_session_id}")

        # Track results
        if isinstance(event, ResultEvent):
            result_count += 1

        # Convert event to output and write
        output = event_to_output(event, new_session_id or session_id)
        output.query_id = query_id
        write_output(output)

    return new_session_id, result_count, closed_during_query


def apply_followup_metadata(
    core_config: AgentCoreConfig,
    *,
    turn_id: str | None,
    query_id: str | None = None,
    metadata: dict[str, object],
) -> None:
    if turn_id is None and query_id is None and not metadata:
        return

    merged = dict(core_config.extra.get("metadata") or {})
    merged.update(metadata)
    if turn_id is not None:
        core_config.turn_id = turn_id
        merged.update(_turn_metadata(turn_id, core_config.chat_jid, core_config.group_folder))
    core_config.extra["metadata"] = merged
    if query_id is not None:
        core_config.extra["pynchy_query_id"] = query_id


def _query_id(core_config: AgentCoreConfig) -> str | None:
    value = core_config.extra.get("pynchy_query_id")
    return value if isinstance(value, str) else None


async def _drive_conversation_loop(
    core: AgentCore,
    core_config: AgentCoreConfig,
    prompt: str,
    session_id: str | None,
) -> None:
    """Run the conversation loop until the host closes the IPC channel."""
    while True:
        new_session_id, result_count, closed_during_query = await _run_single_query(
            core,
            prompt,
            session_id,
            query_id=_query_id(core_config),
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
        write_output(
            ContainerOutput(
                status="success",
                result=None,
                new_session_id=session_id,
                query_id=_query_id(core_config),
            )
        )

        log("Query ended, waiting for next IPC message...")

        followup = await wait_for_ipc_followup()
        if followup is None:
            log("Close sentinel received, exiting")
            break

        apply_followup_metadata(
            core_config,
            turn_id=followup.turn_id,
            query_id=followup.query_id,
            metadata=followup.metadata,
        )
        log(f"Got new message ({len(followup.text)} chars), starting new query")
        prompt = followup.text


async def _run_conversation_loop(
    core: AgentCore,
    core_config: AgentCoreConfig,
    prompt: str,
    session_id: str | None,
) -> None:
    """Drive query → wait-for-next-message cycles until the host signals close."""
    try:
        await _drive_conversation_loop(core, core_config, prompt, session_id)

    except Exception as exc:  # allow: exception-handling; loop  # noqa: BLE001
        error_message = str(exc)
        log(f"Agent error: {error_message}")
        write_output(
            ContainerOutput(
                status="error",
                new_session_id=session_id,
                error=error_message,
                query_id=_query_id(core_config),
            )
        )
        sys.exit(1)
    finally:
        try:
            await core.stop()
        except Exception as exc:  # allow: exception-handling; cleanup  # noqa: BLE001
            log(f"Error stopping core: {exc}")


async def main() -> None:
    container_input = _read_container_input()

    # Clean up stale _close sentinel
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        IPC_INPUT_CLOSE_SENTINEL.unlink()

    prompt = build_initial_prompt(container_input)
    core, core_config = await _create_and_start_core(container_input)
    await _run_conversation_loop(core, core_config, prompt, container_input.session_id)
