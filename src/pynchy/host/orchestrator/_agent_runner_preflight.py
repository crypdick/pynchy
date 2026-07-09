"""Pre-container setup helpers for host-side agent orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.config import get_settings
from pynchy.host.container_manager import (
    OnOutput,
    resolve_agent_core,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.host.container_manager.orchestrator import resolve_container_timeout
from pynchy.host.git_ops.repo import get_repo_context
from pynchy.host.git_ops.utils import count_unpushed_commits, is_repo_dirty
from pynchy.state import get_all_host_jobs, get_all_tasks, set_session
from pynchy.types import ContainerOutput, GroupFolder, SessionId, WorkspaceProfile


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


@runtime_checkable
class _PreflightDeps(Protocol):
    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def _session_cleared(self) -> set[str]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def plugin_manager(self) -> Any: ...

    async def get_available_groups(self) -> list[dict[str, Any]]: ...

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None: ...


def session_id_from_output(output: ContainerOutput) -> str | None:
    """Extract a resumable agent session id from any container output event."""
    if output.new_session_id:
        return output.new_session_id
    if output.type != "system":
        return None
    session_id = (output.system_data or {}).get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


async def _pre_container_setup(
    deps: _PreflightDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    on_output: OnOutput | None,
    extra_system_notices: list[str] | None,
    input_source: str,
    is_scheduled_task: bool,
    repo_access_override: str | None,
) -> _PreContainerResult:
    """Common pre-container setup for both warm and cold paths."""
    del is_scheduled_task  # preflight behavior is identical for scheduled/interactive paths

    is_admin, repo_access, system_prompt_append, session_id = _resolved_pre_container_context(
        deps,
        group.folder,
        group.is_admin,
        repo_access_override=repo_access_override,
    )
    await deps.broadcast_agent_input(chat_jid, messages, source=input_source)
    snapshot_ms = await _write_container_snapshots(
        deps,
        group.folder,
        is_admin=is_admin,
    )
    wrapped_on_output = _session_tracking_output_handler(deps, group.folder, on_output)
    system_notices = _merged_system_notices(
        _build_admin_system_notices(group.folder, is_admin=is_admin, repo_access=repo_access),
        extra_system_notices,
    )

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


def _resolved_pre_container_context(
    deps: _PreflightDeps,
    group_folder: str,
    is_admin: bool,
    *,
    repo_access_override: str | None,
) -> tuple[bool, str | None, str | None, str | None]:
    from pynchy.config.prompts import read_prompts
    from pynchy.host.orchestrator.workspace_config import load_resolved_config

    resolved = load_resolved_config(group_folder)
    repo_access = (
        repo_access_override
        if repo_access_override is not None
        else (resolved.repo_access if resolved else None)
    )
    system_prompt_append = read_prompts(
        resolved.prompts if resolved else [],
        get_settings().project_root,
    )
    session_id = deps.sessions.get(group_folder)
    return is_admin, repo_access, system_prompt_append, session_id


async def _write_container_snapshots(
    deps: _PreflightDeps,
    group_folder: str,
    *,
    is_admin: bool,
) -> float:
    snapshot_start = time.monotonic()
    tasks = await get_all_tasks()
    host_jobs = await get_all_host_jobs() if is_admin else []
    write_tasks_snapshot(
        group_folder,
        is_admin,
        [t.to_snapshot_dict() for t in tasks],
        host_jobs=[j.to_snapshot_dict() for j in host_jobs],
    )

    available_groups = await deps.get_available_groups()
    write_groups_snapshot(
        group_folder,
        is_admin,
        available_groups,
        set(deps.workspaces.keys()),
    )
    return (time.monotonic() - snapshot_start) * 1000


def _session_tracking_output_handler(
    deps: _PreflightDeps,
    group_folder: str,
    on_output: OnOutput | None,
) -> OnOutput:
    async def wrapped_on_output(output: ContainerOutput) -> None:
        if (
            session_id := session_id_from_output(output)
        ) and group_folder not in deps._session_cleared:
            deps.sessions[group_folder] = session_id
            await set_session(GroupFolder(group_folder), SessionId(session_id))
        if on_output:
            await on_output(output)

    return wrapped_on_output


def _build_admin_system_notices(
    group_folder: str,
    *,
    is_admin: bool,
    repo_access: str | None,
) -> list[str]:
    if not is_admin:
        return []

    repo_ctx = get_repo_context(repo_access) if repo_access else None
    check_cwd = repo_ctx.worktrees_dir / group_folder if repo_ctx else None
    system_notices: list[str] = []
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
    return system_notices


def _merged_system_notices(
    system_notices: list[str],
    extra_system_notices: list[str] | None,
) -> list[str]:
    if not extra_system_notices:
        return system_notices
    return [*system_notices, *extra_system_notices]
