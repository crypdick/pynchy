"""Agent execution orchestration — snapshot writes, session tracking, container launch.

Supports two execution paths:
  Cold path: first message or after reset — spawn container, create session
  Warm path: subsequent messages — send via IPC to existing session
  One-shot: scheduled tasks — always spawn fresh, no persistent session
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.config import get_settings
from pynchy.container_runner import (
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
    run_container_agent,
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


async def _pre_container_setup(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict],
    on_output: Any | None,
    extra_system_notices: list[str] | None,
    input_source: str,
    is_scheduled_task: bool,
    repo_access_override: str | None,
) -> tuple:
    """Common pre-container setup for both warm and cold paths.

    Returns (is_admin, repo_access, system_prompt_append, session_id,
             system_notices, agent_core_module, agent_core_class,
             wrapped_on_output, config_timeout, snapshot_ms).
    """
    from pynchy.directives import resolve_directives
    from pynchy.workspace_config import get_repo_access

    s = get_settings()
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

    config_timeout = (
        group.container_config.timeout
        if group.container_config and group.container_config.timeout
        else s.container_timeout
    )

    return (
        is_admin,
        repo_access,
        system_prompt_append,
        session_id,
        system_notices,
        agent_core_module,
        agent_core_class,
        wrapped_on_output,
        config_timeout,
        snapshot_ms,
    )


# ---------------------------------------------------------------------------
# Warm path — reuse existing session
# ---------------------------------------------------------------------------


async def _warm_query(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    session: Any,  # ContainerSession
    messages: list[dict],
    system_notices: list[str],
    wrapped_on_output: Any,
    config_timeout: float,
) -> str:
    """Send messages to an existing session via IPC and wait for completion."""
    # Ensure MCP servers are running (they may have stopped since last query)
    from pynchy.container_runner.mcp_manager import get_mcp_manager

    mcp_mgr = get_mcp_manager()
    if mcp_mgr is not None:
        instance_ids = mcp_mgr.get_workspace_instance_ids(group.folder)
        for iid in instance_ids:
            try:
                await mcp_mgr.ensure_running(iid)
            except (TimeoutError, RuntimeError):
                logger.warning("Failed to start MCP instance", instance_id=iid, group=group.folder)

    # Register the session's process so send_message() works for follow-ups
    deps.queue.register_process(chat_jid, session.proc, session.container_name, group.folder)

    # Set output handler and format messages
    session.set_output_handler(wrapped_on_output)
    formatted = _format_messages_for_ipc(messages, system_notices or None)

    # Send via IPC
    await session.send_ipc_message(formatted)

    # Wait for query completion
    try:
        await session.wait_for_query_done(timeout=config_timeout)
    except TimeoutError:
        logger.error("Warm query timed out, destroying session", group=group.name)
        await destroy_session(group.folder)
        return "error"
    except SessionDiedError:
        logger.error("Container died during warm query", group=group.name)
        return "error"

    return "success"


# ---------------------------------------------------------------------------
# Cold path — spawn new container and create session
# ---------------------------------------------------------------------------


async def _cold_start(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict],
    is_admin: bool,
    repo_access: str | None,
    system_prompt_append: str | None,
    session_id: str | None,
    system_notices: list[str],
    agent_core_module: str,
    agent_core_class: str,
    wrapped_on_output: Any,
    config_timeout: float,
) -> str:
    """Spawn a new container, create a persistent session, and wait for the first query."""
    container_name = stable_container_name(group.folder)
    input_data = ContainerInput(
        messages=messages,
        session_id=session_id,
        group_folder=group.folder,
        chat_jid=chat_jid,
        is_admin=is_admin,
        system_notices=system_notices or None,
        repo_access=repo_access,
        system_prompt_append=system_prompt_append,
        agent_core_module=agent_core_module,
        agent_core_class=agent_core_class,
    )

    try:
        proc, container_name, _mounts = await _spawn_container(
            group, input_data, container_name, deps.plugin_manager
        )
    except OSError as exc:
        logger.error("Failed to spawn container", error=str(exc), container=container_name)
        return "error"

    # Determine idle timeout from workspace config
    from pynchy.workspace_config import load_workspace_config

    ws_config = load_workspace_config(group.folder)
    idle_enabled = ws_config.idle_terminate if ws_config else True
    idle_timeout = get_settings().idle_timeout if idle_enabled else 0.0

    session = await create_session(
        group.folder,
        container_name,
        proc,
        idle_timeout_override=idle_timeout,
    )

    # Register process so send_message() works for follow-ups during this query
    deps.queue.register_process(chat_jid, proc, container_name, group.folder)

    # Set output handler and wait
    session.set_output_handler(wrapped_on_output)

    try:
        await session.wait_for_query_done(timeout=config_timeout)
    except TimeoutError:
        logger.error("Cold start query timed out, destroying session", group=group.name)
        await destroy_session(group.folder)
        return "error"
    except SessionDiedError:
        logger.error("Container died during cold start", group=group.name)
        return "error"

    return "success"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict],
    on_output: Any | None = None,
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

    # --- Scheduled tasks: one-shot container, no persistent session ---
    if is_scheduled_task:
        return await _run_scheduled_task(
            deps,
            group,
            chat_jid,
            messages,
            on_output,
            extra_system_notices,
            repo_access_override,
            input_source,
        )

    # --- Interactive messages: warm/cold session path ---
    (
        is_admin,
        repo_access,
        system_prompt_append,
        session_id,
        system_notices,
        agent_core_module,
        agent_core_class,
        wrapped_on_output,
        config_timeout,
        snapshot_ms,
    ) = await _pre_container_setup(
        deps,
        group,
        chat_jid,
        messages,
        on_output,
        extra_system_notices,
        input_source,
        False,
        repo_access_override,
    )

    session = get_session(group.folder)

    pre_container_ms = (time.monotonic() - run_agent_start) * 1000
    is_warm = session is not None and session.is_alive
    logger.info(
        "run_agent pre-container setup",
        group=group.name,
        snapshot_ms=round(snapshot_ms),
        pre_container_ms=round(pre_container_ms),
        system_notices=len(system_notices),
        has_session=session_id is not None,
        path="warm" if is_warm else "cold",
    )

    try:
        if is_warm:
            return await _warm_query(
                deps,
                group,
                chat_jid,
                session,
                messages,
                system_notices,
                wrapped_on_output,
                config_timeout,
            )
        else:
            return await _cold_start(
                deps,
                group,
                chat_jid,
                messages,
                is_admin,
                repo_access,
                system_prompt_append,
                session_id,
                system_notices,
                agent_core_module,
                agent_core_class,
                wrapped_on_output,
                config_timeout,
            )
    except Exception as exc:
        logger.error("Agent error", group=group.name, err=str(exc))
        return "error"


# ---------------------------------------------------------------------------
# Scheduled task path (one-shot, no persistent session)
# ---------------------------------------------------------------------------


async def _run_scheduled_task(
    deps: AgentRunnerDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict],
    on_output: Any | None,
    extra_system_notices: list[str] | None,
    repo_access_override: str | None,
    input_source: str,
) -> str:
    """Run a scheduled task in a one-shot container, destroying any existing session."""
    # Destroy any persistent session for this group — tasks need a clean slate
    await destroy_session(group.folder)

    (
        is_admin,
        repo_access,
        system_prompt_append,
        session_id,
        system_notices,
        agent_core_module,
        agent_core_class,
        wrapped_on_output,
        config_timeout,
        snapshot_ms,
    ) = await _pre_container_setup(
        deps,
        group,
        chat_jid,
        messages,
        on_output,
        extra_system_notices,
        input_source,
        True,
        repo_access_override,
    )

    logger.info(
        "run_agent scheduled task (one-shot)",
        group=group.name,
        snapshot_ms=round(snapshot_ms),
    )

    try:
        output = await run_container_agent(
            group=group,
            input_data=ContainerInput(
                messages=messages,
                session_id=session_id,
                group_folder=group.folder,
                chat_jid=chat_jid,
                is_admin=is_admin,
                system_notices=system_notices or None,
                is_scheduled_task=True,
                repo_access=repo_access,
                system_prompt_append=system_prompt_append,
                agent_core_module=agent_core_module,
                agent_core_class=agent_core_class,
            ),
            on_process=lambda proc, name: deps.queue.register_process(
                chat_jid, proc, name, group.folder
            ),
            on_output=wrapped_on_output if on_output else None,
            plugin_manager=deps.plugin_manager,
        )

        if output.new_session_id and group.folder not in deps._session_cleared:
            deps.sessions[group.folder] = output.new_session_id
            await set_session(group.folder, output.new_session_id)

        if output.status == "error":
            logger.error(
                "Scheduled task agent error",
                group=group.name,
                error=output.error,
            )
            return "error"

        return "success"
    except Exception as exc:
        logger.error("Scheduled task error", group=group.name, err=str(exc))
        return "error"
