"""Agent execution orchestration — snapshot writes, session tracking, container launch.

Supports two execution paths:
  Cold path: first message or after reset — spawn container, create session
  Warm path: subsequent messages — send via IPC to existing session
  One-shot: scheduled tasks — spawn fresh with session for real-time streaming
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pynchy.config import get_settings
from pynchy.host.container_manager import (
    ContainerSession,
    OnOutput,
    SessionDiedError,
    create_session,
    destroy_session,
    get_session,
)
from pynchy.host.container_manager.orchestrator import (
    _spawn_container,
    oneshot_container_name,
    stable_container_name,
)
from pynchy.host.orchestrator import _agent_runner_preflight as _preflight
from pynchy.logger import logger
from pynchy.state import clear_session
from pynchy.types import ContainerInput, GroupFolder, WorkspaceProfile

if TYPE_CHECKING:
    import pluggy

    from pynchy.host.orchestrator.concurrency import GroupQueue


_PreContainerResult = _preflight._PreContainerResult
_pre_container_setup = _preflight._pre_container_setup
_resolved_pre_container_context = _preflight._resolved_pre_container_context
_write_container_snapshots = _preflight._write_container_snapshots
_session_tracking_output_handler = _preflight._session_tracking_output_handler
_build_admin_system_notices = _preflight._build_admin_system_notices
_merged_system_notices = _preflight._merged_system_notices
session_id_from_output = _preflight.session_id_from_output


@runtime_checkable
class AgentRunnerDeps(Protocol):
    """Dependencies for agent execution."""

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def _session_cleared(self) -> set[str]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def plugin_manager(self) -> pluggy.PluginManager | None: ...

    async def get_available_groups(self) -> list[dict[str, Any]]: ...

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None: ...


# ---------------------------------------------------------------------------
# IPC message formatting
# ---------------------------------------------------------------------------


def _escape_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _format_messages_for_ipc(
    messages: list[dict[str, Any]], system_notices: list[str] | None = None
) -> str:
    """Format messages as XML for IPC delivery to a warm container.

    Replicates the container's build_sdk_messages() format so the agent
    sees the same structure whether messages arrive via stdin (cold) or
    IPC (warm).  System notices are prepended as a <system_notices> block.
    """
    parts: list[str] = []

    if system_notices:
        notice_lines = "\n".join(f"- {n}" for n in system_notices)
        parts.append(f"<system_notices>\n{notice_lines}\n</system_notices>")

    if messages:
        msg_lines: list[str] = []
        for msg in messages:
            sender_name = _escape_xml(msg.get("sender_name", "Unknown"))
            timestamp = msg.get("timestamp", "")
            content = _escape_xml(msg.get("content", ""))
            msg_lines.append(
                f'<message sender="{sender_name}" time="{timestamp}">{content}</message>'
            )
        parts.append(f"<messages>\n{chr(10).join(msg_lines)}\n</messages>")

    return "\n".join(parts)


def _build_container_input(
    messages: list[dict[str, Any]],
    ctx: _PreContainerResult,
    chat_jid: str,
    group: WorkspaceProfile,
    *,
    is_scheduled_task: bool = False,
) -> ContainerInput:
    """Build a ContainerInput from the pre-container result.

    Shared by cold start and scheduled task paths to avoid duplicating
    the field mapping.
    """
    agent_core_config = _agent_core_config_from_settings(group.folder)
    return ContainerInput(
        messages=messages,
        session_id=ctx.session_id,
        group_folder=group.folder,
        chat_jid=chat_jid,
        is_admin=ctx.is_admin,
        system_notices=ctx.system_notices or None,
        is_scheduled_task=is_scheduled_task,
        repo_access=ctx.repo_access,
        repo_accesses=ctx.repo_accesses,
        system_prompt_append=ctx.system_prompt_append,
        agent_core_module=ctx.agent_core_module,
        agent_core_class=ctx.agent_core_class,
        agent_core_config=agent_core_config,
    )


def _agent_core_config_from_settings(group_folder: str | None = None) -> dict[str, str] | None:
    s = get_settings()
    resolved_model = s.agent.model
    if group_folder is not None:
        from pynchy.host.orchestrator.workspace_config import load_resolved_config

        workspace_config = load_resolved_config(group_folder)
        if workspace_config is not None and workspace_config.model:
            resolved_model = workspace_config.model

    result: dict[str, str] = {}
    if resolved_model:
        result["model"] = resolved_model
    return result or None


# ---------------------------------------------------------------------------
# Shared: wait for query completion with timeout/death handling
# ---------------------------------------------------------------------------


async def _await_query(
    session: ContainerSession,
    group: WorkspaceProfile,
    query_timeout_seconds: float,
    label: str,
) -> str:
    """Wait for a session's query to complete. Returns 'success' or 'error'.

    Handles the two expected failure modes:
    - TimeoutError: container unresponsive — destroy the session.
    - SessionDiedError: container exited mid-query — leave cleanup to caller.
    """
    try:
        await session.wait_for_query_done(query_timeout_seconds=query_timeout_seconds)
    except TimeoutError:
        logger.error("query timed out, destroying session", label=label, group=group.name)
        await destroy_session(group.folder)
        return "error"
    except SessionDiedError:
        logger.error("container died during query", label=label, group=group.name)
        return "error"
    return "success"


# ---------------------------------------------------------------------------
# Shared: spawn container → create session → register → await
# ---------------------------------------------------------------------------


async def _spawn_and_await(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    input_data: ContainerInput,
    container_name: str,
    ctx: _PreContainerResult,
    *,
    idle_timeout: float,
    label: str,
) -> str:
    """Spawn a container, create a session, and wait for the query to complete.

    Shared by _cold_start and _run_scheduled_task to avoid duplicating the
    spawn → register → create_session → set_handler → await_query sequence.
    """
    try:
        proc, container_name, _mounts = await _spawn_container(
            group, input_data, container_name, deps.plugin_manager
        )
    except OSError as exc:
        logger.error("Failed to spawn container", error=str(exc), container=container_name)
        return "error"

    session = await create_session(
        group.folder,
        container_name,
        proc,
        idle_timeout_override=idle_timeout,
    )
    deps.queue.register_process(
        chat_jid, proc, container_name, group.folder, input_data.invocation_ts
    )
    session.set_output_handler(ctx.wrapped_on_output)

    return await _await_query(session, group, ctx.config_timeout, label)


# ---------------------------------------------------------------------------
# Warm path — reuse existing session
# ---------------------------------------------------------------------------


async def _warm_query(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    session: ContainerSession,
    messages: list[dict[str, Any]],
    ctx: _PreContainerResult,
) -> str:
    """Send messages to an existing session via IPC and wait for completion."""
    # Ensure MCP servers are running (they may have stopped since last query)
    from pynchy.host.container_manager.mcp.manager import get_mcp_manager

    mcp_mgr = get_mcp_manager()
    if mcp_mgr is not None:
        await mcp_mgr.ensure_workspace_running(group.folder)

    # Register the session's process so send_message() works for follow-ups
    deps.queue.register_process(chat_jid, session.proc, session.container_name, group.folder)

    # Set output handler and format messages
    session.set_output_handler(ctx.wrapped_on_output)
    formatted = _format_messages_for_ipc(messages, ctx.system_notices or None)

    # Send via IPC
    await session.send_ipc_message(formatted)

    return await _await_query(session, group, ctx.config_timeout, "warm query")


# ---------------------------------------------------------------------------
# Cold path — spawn container and create session
# ---------------------------------------------------------------------------


async def _cold_start(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    ctx: _PreContainerResult,
) -> str:
    """Spawn a container, create a persistent session, and wait for the first query."""
    container_name = stable_container_name(group.folder)
    input_data = _build_container_input(messages, ctx, chat_jid, group)

    # Remove stale container with the same name before spawning.
    # After a service restart or container crash, a dead Docker container may
    # still exist with this stable name, causing `docker run` to fail with
    # exit code 125 (name conflict).
    from pynchy.host.container_manager.process import _docker_rm_force

    await _docker_rm_force(container_name)

    idle_timeout = get_settings().idle_timeout

    return await _spawn_and_await(
        deps,
        group,
        chat_jid,
        input_data,
        container_name,
        ctx,
        idle_timeout=idle_timeout,
        label="cold start",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    on_output: OnOutput | None = None,
    extra_system_notices: list[str] | None = None,
    *,
    is_scheduled_task: bool = False,
    repo_access_override: str | None = None,
    input_source: str = "user",
) -> str:
    """Run the container agent for a group. Returns 'success' or 'error'.

    This is the single public entry point for all agent invocations.
    Uses persistent sessions for interactive messages (warm path reuses an
    existing container, cold path spawns one).  Scheduled tasks always
    use one-shot containers.

    Args:
        is_scheduled_task: Whether this is a scheduled task run.
        repo_access_override: Explicit repo_access slug; None = auto-detect from workspace config.
        input_source: Source label for input broadcasting
            ("user", "scheduled_task", "reset_handoff", "hidden_learning_review").
    """
    run_agent_start = time.monotonic()

    # Scheduled tasks need a clean slate — destroy any persistent session first.
    if is_scheduled_task:
        await destroy_session(group.folder)

    # Pre-container setup is shared by all paths (warm, cold, scheduled).
    ctx = await _pre_container_setup(
        deps,
        group,
        chat_jid,
        messages,
        on_output,
        extra_system_notices,
        input_source,
        is_scheduled_task,
        repo_access_override,
    )

    # --- Scheduled tasks: one-shot container, no persistent session ---
    if is_scheduled_task:
        logger.info(
            "run_agent scheduled task (one-shot)",
            group=group.name,
            snapshot_ms=round(ctx.snapshot_ms),
        )
        return await _run_scheduled_task(deps, group, chat_jid, messages, ctx)

    # --- Interactive messages: warm/cold session path ---
    session = get_session(GroupFolder(group.folder))

    pre_container_ms = (time.monotonic() - run_agent_start) * 1000
    is_warm = session is not None and session.is_alive
    logger.info(
        "run_agent pre-container setup",
        group=group.name,
        snapshot_ms=round(ctx.snapshot_ms),
        pre_container_ms=round(pre_container_ms),
        system_notices=len(ctx.system_notices),
        has_session=ctx.session_id is not None,
        path="warm" if is_warm else "cold",
    )

    try:
        if session is not None and session.is_alive:
            return await _warm_query(deps, group, chat_jid, session, messages, ctx)
        return await _cold_start(deps, group, chat_jid, messages, ctx)
    except Exception:
        logger.exception("Agent error", group=group.name)
        return "error"


# ---------------------------------------------------------------------------
# Scheduled task path (one-shot, no persistent session)
# ---------------------------------------------------------------------------


async def _run_scheduled_task(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    ctx: _PreContainerResult,
) -> str:
    """Run a scheduled task in a one-shot container with real-time output streaming.

    Pre-container setup and session teardown are handled by run_agent before
    this is called.  Uses _spawn_and_await for the spawn/session/wait sequence.

    On CancelledError (deploy SIGTERM), the session is preserved so
    deploy_continuation can resume the task on restart.
    """
    input_data = _build_container_input(messages, ctx, chat_jid, group, is_scheduled_task=True)
    container_name = oneshot_container_name(group.folder)
    interrupted = False

    try:
        return await _spawn_and_await(
            deps,
            group,
            chat_jid,
            input_data,
            container_name,
            ctx,
            idle_timeout=get_settings().idle_timeout,
            label="scheduled task",
        )
    except asyncio.CancelledError:
        # Deploy SIGTERM — preserve session for resume on restart.
        # finalize_deploy captures active_sessions BEFORE sending SIGTERM,
        # so the session_id is already in deploy_continuation.json.
        interrupted = True
        raise
    except Exception:
        logger.exception("Scheduled task error", group=group.name)
        return "error"
    finally:
        if not interrupted:
            # Clean up the session created by the one-shot container.
            # Without this, the workspace appears "active" and receives
            # deploy resume messages that trigger unnecessary agent runs.
            await destroy_session(group.folder)
            await clear_session(GroupFolder(group.folder))
            deps.sessions.pop(group.folder, None)
