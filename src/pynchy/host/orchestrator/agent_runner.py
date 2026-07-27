"""Agent execution orchestration — snapshot writes, session tracking, container launch.

Supports two execution paths:
  Cold path: first message or after reset — spawn container, create session
  Warm path: subsequent messages — send via IPC to existing session
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pynchy.host.container_manager.mcp.manager as mcp_manager
import pynchy.host.container_manager.process as container_process
from pynchy.conversation.events import new_turn_id
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
    stable_container_name,
)
from pynchy.host.learning.skill_activation import refresh_personalized_agent_skills
from pynchy.host.orchestrator import _agent_runner_preflight as _preflight
from pynchy.host.orchestrator.agent_core_config import (
    agent_core_config,
)
from pynchy.host.orchestrator.agent_core_config import (
    session_model_mismatch as _session_model_mismatch,
)
from pynchy.host.orchestrator.host_agent_dispatch import run_host_execution as _run_host_execution
from pynchy.host.orchestrator.host_execution import host_execution_cwd as _host_execution_cwd
from pynchy.host.orchestrator.ipc_message_formatting import format_messages_for_ipc
from pynchy.host.orchestrator.mcp_notifications import notify_mcp_startup_failures
from pynchy.logger import logger
from pynchy.state import clear_session
from pynchy.types import (
    AgentExecutionRuntime,
    ContainerInput,
    GroupFolder,
    RuntimeId,
    WorkspaceProfile,
)

if TYPE_CHECKING:
    import pluggy

    from pynchy.host.orchestrator.concurrency import GroupQueue


PreContainerResult = _preflight.PreContainerResult
PreContainerSetupRequest = _preflight.PreContainerSetupRequest
pre_container_setup = _preflight.pre_container_setup
session_tracking_output_handler = _preflight.session_tracking_output_handler
build_admin_system_notices = _preflight.build_admin_system_notices
session_id_from_output = _preflight.session_id_from_output


@runtime_checkable
class AgentRunnerDeps(Protocol):
    """Dependencies for agent execution."""

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def session_cleared(self) -> set[str]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def plugin_manager(self) -> pluggy.PluginManager | None: ...

    @property
    def agent_execution_runtime(self) -> AgentExecutionRuntime: ...

    async def get_available_groups(self) -> list[dict[str, Any]]: ...

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


@dataclass(frozen=True)
class _SpawnAndAwaitRequest:
    deps: AgentRunnerDeps
    group: WorkspaceProfile
    chat_jid: str
    input_data: ContainerInput
    container_name: str
    ctx: PreContainerResult
    idle_timeout: float
    label: str
    runtime: AgentExecutionRuntime


@dataclass(frozen=True)
class _WarmQueryRequest:
    deps: AgentRunnerDeps
    group: WorkspaceProfile
    chat_jid: str
    session: ContainerSession
    messages: list[dict[str, Any]]
    ctx: PreContainerResult


def _turn_metadata(turn_id: str, chat_jid: str, group_folder: str) -> dict[str, str]:
    return {
        "pynchy_turn_id": turn_id,
        "pynchy_chat_jid": chat_jid,
        "pynchy_group_folder": group_folder,
    }


def build_container_input(  # noqa: PLR0913, RUF100 - explicit runner wire inputs keep this boundary inspectable.
    messages: list[dict[str, Any]],
    ctx: PreContainerResult,
    chat_jid: str,
    group: WorkspaceProfile,
    *,
    runtime: AgentExecutionRuntime,
    is_scheduled_task: bool = False,
) -> ContainerInput:
    """Build a runner input through this module's patchable settings seam."""
    return _preflight.build_container_input(
        messages,
        ctx,
        chat_jid,
        group,
        agent_core_config=agent_core_config(
            runtime.model, runtime.model_reasoning_effort, group.folder
        ),
        is_scheduled_task=is_scheduled_task,
    )


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


async def _spawn_and_await(request: _SpawnAndAwaitRequest) -> str:
    """Spawn a container, create a session, and wait for the query to complete.

    Keeps the spawn → register → create_session → set_handler → await-query
    sequence together for every durable cold start.
    """
    try:
        proc, container_name, _mounts, mcp_startup_failures = await _spawn_container(
            request.group,
            request.input_data,
            request.container_name,
            request.deps.plugin_manager,
            request.runtime,
        )
    except OSError as exc:
        logger.error("Failed to spawn container", error=str(exc), container=request.container_name)
        return "error"

    if mcp_startup_failures:
        await notify_mcp_startup_failures(
            request.deps.broadcast_host_message,
            request.chat_jid,
            mcp_startup_failures,
        )

    session = await create_session(
        request.group.folder,
        container_name,
        proc,
        data_dir=request.runtime.data_dir,
        idle_timeout=request.idle_timeout,
        invocation_ts=request.input_data.invocation_ts,
    )
    registered = request.deps.queue.register_process(
        RuntimeId(request.group.folder),
        proc,
        container_name,
        request.input_data.invocation_ts,
    )
    if not registered:
        await destroy_session(request.group.folder)
        return "interrupted"
    session.set_output_handler(
        request.ctx.wrapped_on_output,
        query_id=request.input_data.query_id,
    )

    return await _await_query(session, request.group, request.ctx.config_timeout, request.label)


# ---------------------------------------------------------------------------
# Warm path — reuse existing session
# ---------------------------------------------------------------------------


async def _warm_query(request: _WarmQueryRequest) -> str:
    """Send messages to an existing session via IPC and wait for completion."""
    refresh_personalized_agent_skills(request.group.folder)

    # Ensure MCP servers are running (they may have stopped since last query)
    mcp_mgr = mcp_manager.get_mcp_manager()
    if mcp_mgr is not None:
        mcp_startup = await mcp_mgr.ensure_workspace_running(request.group.folder)
        if mcp_startup.failures:
            await notify_mcp_startup_failures(
                request.deps.broadcast_host_message,
                request.chat_jid,
                mcp_startup.failures,
            )

    # Register the session's process so send_message() works for follow-ups
    registered = request.deps.queue.register_process(
        RuntimeId(request.group.folder),
        request.session.proc,
        request.session.container_name,
    )
    if not registered:
        return "interrupted"

    # Bind output/progress to this query before sending its IPC message.
    turn_id = request.ctx.turn_id or new_turn_id()
    query_id = new_turn_id()
    request.session.set_output_handler(
        request.ctx.wrapped_on_output,
        query_id=query_id,
    )
    formatted = format_messages_for_ipc(request.messages, request.ctx.system_notices or None)

    # Send via IPC
    await request.session.send_ipc_message(
        formatted,
        turn_id=turn_id,
        query_id=query_id,
        metadata=_turn_metadata(turn_id, request.chat_jid, request.group.folder),
    )

    return await _await_query(
        request.session, request.group, request.ctx.config_timeout, "warm query"
    )


# ---------------------------------------------------------------------------
# Cold path — spawn container and create session
# ---------------------------------------------------------------------------


async def _cold_start(  # noqa: PLR0913, RUF100 - cold-start values remain explicit at the execution boundary.
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    ctx: PreContainerResult,
    runtime: AgentExecutionRuntime,
) -> str:
    """Spawn a container, create a persistent session, and wait for the first query."""
    container_name = stable_container_name(group.folder)
    input_data = build_container_input(messages, ctx, chat_jid, group, runtime=runtime)

    # Remove stale container with the same name before spawning.
    # After a service restart or container crash, a dead Docker container may
    # still exist with this stable name, causing `docker run` to fail with
    # exit code 125 (name conflict).
    await container_process.docker_rm_force(container_name)

    return await _spawn_and_await(
        _SpawnAndAwaitRequest(
            deps=deps,
            group=group,
            chat_jid=chat_jid,
            input_data=input_data,
            container_name=container_name,
            ctx=ctx,
            idle_timeout=runtime.idle_timeout,
            label="cold start",
            runtime=runtime,
        )
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(  # noqa: PLR0913, RUF100 - public orchestrator entry point preserves the full dependency contract.
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
    turn_id: str | None = None,
    resume_session_id: str | None = None,
) -> str:
    """Run the container agent for a group. Returns 'success' or 'error'.

    This is the single public entry point for all agent invocations.
    Every invocation uses the durable session owned by the visible thread.
    A live worker is reused when possible; otherwise a disposable worker
    resumes the stored provider session.

    Args:
        is_scheduled_task: Whether this is a scheduled task run.
        repo_access_override: Explicit repo_access slug; None = auto-detect from workspace config.
        input_source: Source label for input broadcasting
            ("user", "scheduled_task", "webhook:<provider>", "reset_handoff",
            "hidden_learning_review"). Webhook sources also taint the invocation as
            public-source input.
    """
    run_agent_start = time.monotonic()
    resolved_turn_id = turn_id or new_turn_id()
    runtime = deps.agent_execution_runtime

    # Pre-container setup is shared by all durable worker paths.
    ctx = await pre_container_setup(
        PreContainerSetupRequest(
            deps=deps,
            group=group,
            chat_jid=chat_jid,
            messages=messages,
            on_output=on_output,
            extra_system_notices=extra_system_notices,
            input_source=input_source,
            is_scheduled_task=is_scheduled_task,
            repo_access_override=repo_access_override,
            runtime=runtime,
        )
    )
    ctx.turn_id = resolved_turn_id
    if resume_session_id is not None:
        ctx.session_id = resume_session_id

    resolved_agent_core_config = agent_core_config(
        runtime.model, runtime.model_reasoning_effort, group.folder
    )
    if _session_model_mismatch(ctx.session_id, resolved_agent_core_config):
        logger.info(
            "Stored Codex session model changed; starting fresh session",
            group=group.name,
            session_id=ctx.session_id,
            model=(resolved_agent_core_config or {}).get("model"),
        )
        await destroy_session(group.folder)
        await clear_session(GroupFolder(group.folder))
        deps.sessions.pop(group.folder, None)
        ctx.session_id = None

    host_cwd = _host_execution_cwd(group.folder)
    if host_cwd is not None:
        return await _run_host_execution(
            deps,
            group,
            chat_jid,
            messages,
            ctx,
            host_cwd,
            build_container_input,
            runtime,
            is_scheduled_task=is_scheduled_task,
        )

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
            return await _warm_query(
                _WarmQueryRequest(
                    deps=deps,
                    group=group,
                    chat_jid=chat_jid,
                    session=session,
                    messages=messages,
                    ctx=ctx,
                )
            )
        return await _cold_start(deps, group, chat_jid, messages, ctx, runtime)
    except Exception:  # noqa: BLE001, RUF100 - outer agent boundary returns "error"
        logger.exception("Agent error", group=group.name)
        return "error"
