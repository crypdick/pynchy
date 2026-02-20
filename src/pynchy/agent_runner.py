"""Agent execution orchestration — snapshot writes, session tracking, container launch.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from pynchy.container_runner import (
    resolve_agent_core,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.container_runner._orchestrator import run_container_agent
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
    It broadcasts input messages to channels, manages snapshots and sessions,
    and launches the container. Do not call run_container_agent directly.

    Args:
        is_scheduled_task: Whether this is a scheduled task run.
        repo_access_override: Explicit repo_access slug; None = auto-detect from workspace config.
        input_source: Source label for input broadcasting
            ("user", "scheduled_task", "reset_handoff").
    """
    from pynchy.directives import resolve_directives
    from pynchy.workspace_config import get_repo_access

    is_admin = group.is_admin
    if repo_access_override is not None:
        repo_access: str | None = repo_access_override
    else:
        repo_access = get_repo_access(group)
    system_prompt_append = resolve_directives(group.folder, repo_access)
    session_id = deps.sessions.get(group.folder)

    # Broadcast input messages to channels so the UI faithfully represents
    # what the agent sees in its token stream.
    await deps.broadcast_agent_input(chat_jid, messages, source=input_source)

    # Update snapshots for container to read
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

    # Wrap on_output to track session ID from streamed results
    async def wrapped_on_output(output: ContainerOutput) -> None:
        if output.new_session_id and group.folder not in deps._session_cleared:
            deps.sessions[group.folder] = output.new_session_id
            await set_session(group.folder, output.new_session_id)
        if on_output:
            await on_output(output)

    # Build system notices for the LLM (SDK system messages, NOT host messages)
    # These are sent TO the LLM as context, distinct from operational host messages
    system_notices: list[str] = []
    if is_admin:
        # Check the group's worktree (not the main repo) for uncommitted changes
        # and unpushed commits. The agent works inside the worktree, so that's
        # the relevant git state.
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

    # Add any extra system notices passed in
    if extra_system_notices:
        if system_notices:
            system_notices.extend(extra_system_notices)
        else:
            system_notices = extra_system_notices[:]

    # Clear the guard — this container run starts fresh
    deps._session_cleared.discard(group.folder)

    # system_notices are handled via system_prompt in the container (ephemeral context)
    # messages contains the persistent conversation history (with message types)
    # The container appends system_notices to the SDK system_prompt parameter

    agent_core_module, agent_core_class = resolve_agent_core(deps.plugin_manager)

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
                is_scheduled_task=is_scheduled_task,
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
                "Container agent error",
                group=group.name,
                error=output.error,
            )
            return "error"

        return "success"
    except Exception as exc:
        logger.error("Agent error", group=group.name, err=str(exc))
        return "error"
