"""Agent execution orchestration — snapshot writes, session tracking, container launch.

Supports two execution paths:
  Cold path: first message or after reset — spawn container, create session
  Warm path: subsequent messages — send via IPC to existing session
  One-shot: scheduled tasks — spawn fresh with session for real-time streaming
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.config import get_settings
from pynchy.container_runner import (
    ContainerSession,
    OnOutput,
    SessionDiedError,
    create_session,
    destroy_session,
    get_session,
    resolve_agent_core,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.container_runner._orchestrator import (
    _spawn_container,
    oneshot_container_name,
    resolve_container_timeout,
    stable_container_name,
)
from pynchy.db import get_all_host_jobs, get_all_tasks, set_session
from pynchy.git_ops.repo import get_repo_context
from pynchy.git_ops.utils import count_unpushed_commits, is_repo_dirty
from pynchy.logger import logger
from pynchy.types import ContainerInput, ContainerOutput

if TYPE_CHECKING:
    import pluggy

    from pynchy.group_queue import GroupQueue
    from pynchy.types import WorkspaceProfile


@dataclass
class _PreContainerResult:
    """Values produced by _pre_container_setup, consumed by warm/cold/scheduled paths."""

    is_admin: bool
    repo_access: str | None
    system_prompt_append: str | None
    session_id: str | None
    system_notices: list[str]
    agent_core_module: str
    agent_core_class: str
    wrapped_on_output: OnOutput
    config_timeout: float
    snapshot_ms: float


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
        self, chat_jid: str, messages: list[dict], *, source: str = "user"
    ) -> None: ...


# ---------------------------------------------------------------------------
# IPC message formatting
# ---------------------------------------------------------------------------


def _escape_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _format_messages_for_ipc(messages: list[dict], system_notices: list[str] | None = None) -> str:
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


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


def _build_container_input(
    messages: list[dict],
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
    return ContainerInput(
        messages=messages,
        session_id=ctx.session_id,
        group_folder=group.folder,
        chat_jid=chat_jid,
        is_admin=ctx.is_admin,
        system_notices=ctx.system_notices or None,
        is_scheduled_task=is_scheduled_task,
        repo_access=ctx.repo_access,
        system_prompt_append=ctx.system_prompt_append,
        agent_core_module=ctx.agent_core_module,
        agent_core_class=ctx.agent_core_class,
    )


async def _pre_container_setup(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict],
    on_output: OnOutput | None,
    extra_system_notices: list[str] | None,
    input_source: str,
    is_scheduled_task: bool,
    repo_access_override: str | None,
) -> _PreContainerResult:
    """Common pre-container setup for both warm and cold paths."""
    from pynchy.directives import resolve_directives
    from pynchy.workspace_config import get_repo_access

    is_admin = group.is_admin
    if repo_access_override is not None:
        repo_access: str | None = repo_access_override
    else:
        repo_access = get_repo_access(group)
    system_prompt_append = resolve_directives(group.folder, repo_access)
    session_id = deps.sessions.get(group.folder)

    # Broadcast input messages to channels
    await deps.broadcast_agent_input(chat_jid, messages, source=input_source)

    # Update snapshots for container to read
    snapshot_start = time.monotonic()
    tasks = await get_all_tasks()
    host_jobs = await get_all_host_jobs() if is_admin else []
    write_tasks_snapshot(
        group.folder,
        is_admin,
        [t.to_snapshot_dict() for t in tasks],
        host_jobs=[j.to_snapshot_dict() for j in host_jobs],
    )

    available_groups = await deps.get_available_groups()
    write_groups_snapshot(
        group.folder,
        is_admin,
        available_groups,
        set(deps.workspaces.keys()),
    )
    snapshot_ms = (time.monotonic() - snapshot_start) * 1000

    # Wrap on_output to track session ID
    async def wrapped_on_output(output: ContainerOutput) -> None:
        if output.new_session_id and group.folder not in deps._session_cleared:
            deps.sessions[group.folder] = output.new_session_id
            await set_session(group.folder, output.new_session_id)
        if on_output:
            await on_output(output)

    # Build system notices
    system_notices: list[str] = []
    if is_admin:
        repo_ctx = get_repo_context(repo_access) if repo_access else None
        check_cwd = repo_ctx.worktrees_dir / group.folder if repo_ctx else None
        if is_repo_dirty(cwd=check_cwd):
            system_notices.append(
                "There are uncommitted local changes. Run `git status` and `git diff` "
                "to review them. If they are good, commit and push. If not, discard them."
            )
        if count_unpushed_commits(cwd=check_cwd) > 0:
            system_notices.append(
                "There are local commits that haven't been pushed. "
                "Run `git push` or `git rebase origin/main && git push` to sync them."
            )
        if system_notices:
            system_notices.append(
                "Consider whether to address these issues before or after handling the new message."
            )

    if extra_system_notices:
        if system_notices:
            system_notices.extend(extra_system_notices)
        else:
            system_notices = extra_system_notices[:]

    deps._session_cleared.discard(group.folder)

    agent_core_module, agent_core_class = resolve_agent_core(deps.plugin_manager)

    config_timeout = resolve_container_timeout(group)

    return _PreContainerResult(
        is_admin=is_admin,
        repo_access=repo_access,
        system_prompt_append=system_prompt_append,
        session_id=session_id,
        system_notices=system_notices,
        agent_core_module=agent_core_module,
        agent_core_class=agent_core_class,
        wrapped_on_output=wrapped_on_output,
        config_timeout=config_timeout,
        snapshot_ms=snapshot_ms,
    )


# ---------------------------------------------------------------------------
# Shared: wait for query completion with timeout/death handling
# ---------------------------------------------------------------------------


async def _await_query(
    session: ContainerSession,
    group: WorkspaceProfile,
    timeout: float,
    label: str,
) -> str:
    """Wait for a session's query to complete. Returns 'success' or 'error'.

    Handles the two expected failure modes:
    - TimeoutError: container unresponsive — destroy the session.
    - SessionDiedError: container exited mid-query — leave cleanup to caller.
    """
    try:
        await session.wait_for_query_done(timeout=timeout)
    except TimeoutError:
        logger.error(f"{label} timed out, destroying session", group=group.name)
        await destroy_session(group.folder)
        return "error"
    except SessionDiedError:
        logger.error(f"Container died during {label}", group=group.name)
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
    messages: list[dict],
    ctx: _PreContainerResult,
) -> str:
    """Send messages to an existing session via IPC and wait for completion."""
    # Ensure MCP servers are running (they may have stopped since last query)
    from pynchy.container_runner.mcp_manager import get_mcp_manager

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
# Cold path — spawn new container and create session
# ---------------------------------------------------------------------------


async def _cold_start(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict],
    ctx: _PreContainerResult,
) -> str:
    """Spawn a new container, create a persistent session, and wait for the first query."""
    container_name = stable_container_name(group.folder)
    input_data = _build_container_input(messages, ctx, chat_jid, group)

    # Remove stale container with the same name before spawning.
    # After a service restart or container crash, a dead Docker container may
    # still exist with this stable name, causing `docker run` to fail with
    # exit code 125 (name conflict).
    from pynchy.container_runner._process import _docker_rm_force

    await _docker_rm_force(container_name)

    # Determine idle timeout from workspace config
    from pynchy.workspace_config import load_workspace_config

    ws_config = load_workspace_config(group.folder)
    idle_enabled = ws_config.idle_terminate if ws_config else True
    idle_timeout = get_settings().idle_timeout if idle_enabled else 0.0

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
    messages: list[dict],
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
    existing container, cold path spawns a new one).  Scheduled tasks always
    use one-shot containers.

    Args:
        is_scheduled_task: Whether this is a scheduled task run.
        repo_access_override: Explicit repo_access slug; None = auto-detect from workspace config.
        input_source: Source label for input broadcasting
            ("user", "scheduled_task", "reset_handoff").
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
    session = get_session(group.folder)

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
        if is_warm:
            return await _warm_query(deps, group, chat_jid, session, messages, ctx)
        else:
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
    messages: list[dict],
    ctx: _PreContainerResult,
) -> str:
    """Run a scheduled task in a one-shot container with real-time output streaming.

    Pre-container setup and session teardown are handled by run_agent before
    this is called.  Uses _spawn_and_await for the spawn/session/wait sequence.
    """
    input_data = _build_container_input(messages, ctx, chat_jid, group, is_scheduled_task=True)
    container_name = oneshot_container_name(group.folder)

    try:
        return await _spawn_and_await(
            deps,
            group,
            chat_jid,
            input_data,
            container_name,
            ctx,
            idle_timeout=0.0,
            label="scheduled task",
        )
    except Exception:
        logger.exception("Scheduled task error", group=group.name)
        return "error"
    finally:
        # Clean up the session created by the one-shot container.
        # Without this, the workspace appears "active" and receives
        # deploy resume messages that trigger unnecessary agent runs.
        await destroy_session(group.folder)
        deps.sessions.pop(group.folder, None)
