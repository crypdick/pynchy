"""Agent execution orchestration — snapshot writes, session tracking, container launch.

Supports two execution paths:
  Cold path: first message or after reset — spawn container, create session
  Warm path: subsequent messages — send via IPC to existing session
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import pluggy

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
    McpStartupFailure,
    OnOutput,
)
from pynchy.conversation.api import new_turn_id
from pynchy.host.orchestrator import _agent_runner_preflight as _preflight
from pynchy.host.orchestrator.agent_core_config import (
    agent_core_config,
)
from pynchy.host.orchestrator.agent_core_config import (
    session_model_mismatch as _session_model_mismatch,
)
from pynchy.host.orchestrator.host_agent_dispatch import (
    BuildContainerInput,
)
from pynchy.host.orchestrator.host_agent_dispatch import run_host_execution as _run_host_execution
from pynchy.host.orchestrator.host_execution import (
    HostExecutionCwdError,
    bind_active_routed_host_repo,
    clear_active_routed_host_repo,
)
from pynchy.host.orchestrator.host_execution import (
    host_execution_cwd as _host_execution_cwd,
)
from pynchy.host.orchestrator.ipc_message_formatting import format_messages_for_ipc
from pynchy.host.orchestrator.mcp_notifications import notify_mcp_startup_failures
from pynchy.identifiers import (
    GroupFolder,
    RuntimeId,
)
from pynchy.logger import logger
from pynchy.state.api import clear_session
from pynchy.workspace.api import (
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.concurrency import GroupQueue
    from pynchy.host.orchestrator.host_execution import HostRuntimeOperations


PreContainerResult = _preflight.PreContainerResult
PreContainerSetupRequest = _preflight.PreContainerSetupRequest
pre_container_setup = _preflight.pre_container_setup
session_tracking_output_handler = _preflight.session_tracking_output_handler
build_admin_system_notices = _preflight.build_admin_system_notices
session_id_from_output = _preflight.session_id_from_output
append_post_work_prompt = _preflight.append_post_work_prompt


@runtime_checkable
class AgentSession(Protocol):
    """The persistent worker behavior required by agent orchestration."""

    @property
    def proc(self) -> asyncio.subprocess.Process: ...

    @property
    def container_name(self) -> str: ...

    @property
    def is_alive(self) -> bool: ...

    def set_output_handler(
        self, on_output: OnOutput | None, *, query_id: str | None = None
    ) -> None: ...

    async def send_ipc_message(
        self,
        text: str,
        *,
        turn_id: str,
        query_id: str,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def wait_for_query_done(self, *, query_timeout_seconds: float) -> None: ...


@dataclass
class ContainerAgentOperations:
    """Container capabilities selected by the application composition root."""

    get_session: Callable[[GroupFolder], object | None]
    start_session: Callable[
        [WorkspaceProfile, ContainerInput, AgentExecutionRuntime, pluggy.PluginManager | None],
        Awaitable[tuple[object, tuple[McpStartupFailure, ...]]],
    ]
    destroy_session: Callable[[str], Awaitable[None]]
    ensure_workspace_mcp: Callable[[str], Awaitable[tuple[McpStartupFailure, ...]]]
    wait_for_query: Callable[[object, float], Awaitable[bool]]


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

    @property
    def host_runtime_operations(self) -> HostRuntimeOperations: ...

    @property
    def container_agent_operations(self) -> ContainerAgentOperations: ...

    async def get_available_groups(self) -> list[dict[str, Any]]: ...

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def refresh_personalized_agent_skills(self, group_folder: str) -> None: ...

    def admin_repo_notices(
        self, group_folder: str, *, is_admin: bool, repo_access: str | None
    ) -> list[str]: ...


@dataclass(frozen=True)
class _WarmQueryRequest:
    deps: AgentRunnerDeps
    operations: ContainerAgentOperations
    group: WorkspaceProfile
    chat_jid: str
    session: AgentSession
    messages: list[dict[str, Any]]
    ctx: PreContainerResult


def _turn_metadata(turn_id: str, chat_jid: str, group_folder: str) -> dict[str, str]:
    return {
        "pynchy_turn_id": turn_id,
        "pynchy_chat_jid": chat_jid,
        "pynchy_group_folder": group_folder,
    }


def build_container_input(  # noqa: PLR0913 - explicit runner wire inputs keep this boundary inspectable.
    messages: list[dict[str, Any]],
    ctx: PreContainerResult,
    chat_jid: str,
    group: WorkspaceProfile,
    *,
    runtime: AgentExecutionRuntime,
    is_scheduled_task: bool = False,
    model_reasoning_effort_override: str | None = None,
) -> ContainerInput:
    """Build a runner input through this module's patchable settings seam."""
    return _preflight.build_container_input(
        messages,
        ctx,
        chat_jid,
        group,
        agent_core_config=agent_core_config(
            runtime.model,
            runtime.model_reasoning_effort,
            group.folder,
            model_reasoning_effort_override=model_reasoning_effort_override,
        ),
        is_scheduled_task=is_scheduled_task,
    )


# ---------------------------------------------------------------------------
# Shared: wait for query completion with timeout/death handling
# ---------------------------------------------------------------------------


async def _await_query(
    operations: ContainerAgentOperations,
    session: object,
    group: WorkspaceProfile,
    query_timeout_seconds: float,
    label: str,
) -> str:
    """Wait for a session's query to complete. Returns 'success' or 'error'.

    Handles the two expected failure modes:
    - TimeoutError: container unresponsive — destroy the session.
    - A dead worker: reported by the container operation without extra cleanup.
    """
    try:
        completed = await operations.wait_for_query(session, query_timeout_seconds)
    except TimeoutError:
        logger.error("query timed out, destroying session", label=label, group=group.name)
        await operations.destroy_session(group.folder)
        return "error"
    if not completed:
        logger.error("container died during query", label=label, group=group.name)
        return "error"
    return "success"


# ---------------------------------------------------------------------------
# Warm path — reuse existing session
# ---------------------------------------------------------------------------


async def _warm_query(request: _WarmQueryRequest) -> str:
    """Send messages to an existing session via IPC and wait for completion."""
    request.deps.refresh_personalized_agent_skills(request.group.folder)

    # Ensure MCP servers are running (they may have stopped since last query)
    mcp_startup_failures = await request.operations.ensure_workspace_mcp(request.group.folder)
    if mcp_startup_failures:
        await notify_mcp_startup_failures(
            request.deps.broadcast_host_message,
            request.chat_jid,
            mcp_startup_failures,
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
        request.operations,
        request.session,
        request.group,
        request.ctx.config_timeout,
        "warm query",
    )


# ---------------------------------------------------------------------------
# Cold path — spawn container and create session
# ---------------------------------------------------------------------------


async def _cold_start(  # noqa: PLR0913 - cold-start values remain explicit at the execution boundary.
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    ctx: PreContainerResult,
    runtime: AgentExecutionRuntime,
    operations: ContainerAgentOperations,
    build_input: BuildContainerInput,
) -> str:
    """Spawn a container, create a persistent session, and wait for the first query."""
    input_data = build_input(messages, ctx, chat_jid, group, runtime=runtime)
    try:
        started, failures = await operations.start_session(
            group, input_data, runtime, deps.plugin_manager
        )
    except OSError as exc:
        logger.error("Failed to start container session", error=str(exc), group=group.folder)
        return "error"
    session = cast("AgentSession", started)
    if not deps.queue.register_process(
        RuntimeId(group.folder), session.proc, session.container_name, input_data.invocation_ts
    ):
        await operations.destroy_session(group.folder)
        return "interrupted"
    session.set_output_handler(ctx.wrapped_on_output, query_id=input_data.query_id)
    if failures:
        await notify_mcp_startup_failures(deps.broadcast_host_message, chat_jid, failures)
    return await _await_query(operations, session, group, ctx.config_timeout, "cold start")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(  # noqa: PLR0913 - public orchestrator entry point preserves the full dependency contract.
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
    automation_memory_dir: Path | None = None,
    model_reasoning_effort_override: str | None = None,
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
    operations = deps.container_agent_operations
    build_input = partial(
        build_container_input,
        model_reasoning_effort_override=model_reasoning_effort_override,
    )

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
            automation_memory_dir=(
                str(automation_memory_dir) if automation_memory_dir is not None else None
            ),
            repo_access_override=repo_access_override,
            runtime=runtime,
        )
    )
    ctx.turn_id = resolved_turn_id
    messages = append_post_work_prompt(messages, ctx.post_work_prompt)
    if resume_session_id is not None:
        ctx.session_id = resume_session_id
    recovered_host_session = ctx.session_id is not None

    resolved_agent_core_config = agent_core_config(
        runtime.model,
        runtime.model_reasoning_effort,
        group.folder,
        model_reasoning_effort_override=model_reasoning_effort_override,
    )
    if _session_model_mismatch(ctx.session_id, resolved_agent_core_config):
        logger.info(
            "Stored Codex session model changed; starting fresh session",
            group=group.name,
            session_id=ctx.session_id,
            model=(resolved_agent_core_config or {}).get("model"),
        )
        await operations.destroy_session(group.folder)
        await clear_session(GroupFolder(group.folder))
        deps.sessions.pop(group.folder, None)
        ctx.session_id = None

    try:
        host_cwd = await asyncio.to_thread(
            _host_execution_cwd,
            group.folder,
            deps.host_runtime_operations,
            repo_accesses=ctx.repo_accesses,
            recovered=recovered_host_session,
        )
    except HostExecutionCwdError as exc:
        logger.error("Host execution working directory blocked", group=group.name, error=str(exc))
        await deps.broadcast_host_message(chat_jid, f"Host execution blocked: {exc}")
        return "error"
    if host_cwd is not None:
        ctx.system_notices.extend(host_cwd.notices)
        if host_cwd.repo_access is not None:
            bind_active_routed_host_repo(group.folder, host_cwd.repo_access, resolved_turn_id)
        try:
            return await _run_host_execution(
                deps,
                group,
                chat_jid,
                messages,
                ctx,
                host_cwd.path,
                build_input,
                runtime,
                is_scheduled_task=is_scheduled_task,
                destroy_session=operations.destroy_session,
            )
        finally:
            if host_cwd.repo_access is not None:
                clear_active_routed_host_repo(group.folder, host_cwd.repo_access, resolved_turn_id)

    session = cast("AgentSession | None", operations.get_session(GroupFolder(group.folder)))
    if session is not None and session.is_alive and is_scheduled_task:
        # A scheduled occurrence needs its exact task-owned mount set even when
        # this durable thread previously ran with memory enabled or disabled.
        await operations.destroy_session(group.folder)
        session = None

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
                    operations=operations,
                    group=group,
                    chat_jid=chat_jid,
                    session=session,
                    messages=messages,
                    ctx=ctx,
                )
            )
        return await _cold_start(
            deps,
            group,
            chat_jid,
            messages,
            ctx,
            runtime,
            operations,
            build_input,
        )
    except Exception:  # noqa: BLE001 - outer agent boundary returns "error"
        logger.exception("Agent error", group=group.name)
        return "error"
