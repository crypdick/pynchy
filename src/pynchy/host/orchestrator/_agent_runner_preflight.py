"""Pre-container setup helpers for host-side agent orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

import pynchy.config.prompts as prompt_config
import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config import get_settings
from pynchy.config.personalization import PersonalizationPaths
from pynchy.conversation.events import new_turn_id
from pynchy.host.container_manager import (
    OnOutput,
    resolve_agent_core,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.host.container_manager.orchestrator import resolve_container_timeout
from pynchy.host.git_ops.repo import get_repo_context
from pynchy.host.git_ops.utils import count_unpushed_commits, is_repo_dirty
from pynchy.state import (
    get_all_host_jobs,
    get_all_tasks,
    get_conversation_control_by_thread,
    set_conversation_session,
    set_session,
    update_in_flight_session,
)
from pynchy.types import (
    ChatJid,
    ContainerInput,
    ContainerOutput,
    GroupFolder,
    SessionId,
    WorkspaceProfile,
)


@dataclass
class PreContainerResult:
    """Values produced by pre_container_setup, consumed by warm/cold/scheduled paths."""

    is_admin: bool
    repo_access: str | None
    repo_accesses: list[str]
    system_prompt_append: str | None
    session_id: str | None
    system_notices: list[str]
    agent_core_module: str
    agent_core_class: str
    wrapped_on_output: OnOutput
    config_timeout: float
    snapshot_ms: float
    turn_id: str | None = None
    input_source: str = "user"


@dataclass(frozen=True)
class PreContainerSetupRequest:
    deps: _PreflightDeps
    group: WorkspaceProfile
    chat_jid: str
    messages: list[dict[str, Any]]
    on_output: OnOutput | None
    extra_system_notices: list[str] | None
    input_source: str
    is_scheduled_task: bool
    repo_access_override: str | None


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
    agent_core_config: dict[str, Any] | None,
    is_scheduled_task: bool = False,
) -> ContainerInput:
    """Build the runner input shared by host, container, and scheduled paths."""
    resolved_turn_id = ctx.turn_id or new_turn_id()
    resolved_core_config = dict(agent_core_config or {})
    metadata = dict(resolved_core_config.get("metadata") or {})
    metadata.update(_turn_metadata(resolved_turn_id, chat_jid, group.folder))
    resolved_core_config["metadata"] = metadata
    return ContainerInput(
        messages=messages,
        turn_id=resolved_turn_id,
        session_id=ctx.session_id,
        group_folder=group.folder,
        chat_jid=chat_jid,
        is_admin=ctx.is_admin,
        system_notices=ctx.system_notices or None,
        is_scheduled_task=is_scheduled_task,
        input_source=ctx.input_source,
        repo_access=ctx.repo_access,
        repo_accesses=ctx.repo_accesses,
        system_prompt_append=ctx.system_prompt_append,
        agent_core_module=ctx.agent_core_module,
        agent_core_class=ctx.agent_core_class,
        agent_core_config=resolved_core_config,
    )


@runtime_checkable
class _PreflightDeps(Protocol):
    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def session_cleared(self) -> set[str]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def plugin_manager(self) -> object: ...

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


async def pre_container_setup(request: PreContainerSetupRequest) -> PreContainerResult:
    """Common pre-container setup for both warm and cold paths."""
    is_admin, repo_access, repo_accesses, system_prompt_append, session_id = (
        resolved_pre_container_context(
            request.deps,
            request.group.folder,
            is_admin=request.group.is_admin,
            repo_access_override=request.repo_access_override,
        )
    )
    await request.deps.broadcast_agent_input(
        request.chat_jid, request.messages, source=request.input_source
    )
    snapshot_ms = await write_container_snapshots(
        request.deps,
        request.group.folder,
        is_admin=is_admin,
    )
    wrapped_on_output = session_tracking_output_handler(
        request.deps,
        request.group.folder,
        request.chat_jid,
        request.on_output,
    )
    system_notices = merged_system_notices(
        build_admin_system_notices(
            request.group.folder, is_admin=is_admin, repo_access=repo_access
        ),
        request.extra_system_notices,
    )

    request.deps.session_cleared.discard(request.group.folder)
    agent_core_module, agent_core_class = resolve_agent_core(
        cast("Any", request.deps.plugin_manager)
    )
    config_timeout = resolve_container_timeout(request.group)

    return PreContainerResult(
        is_admin=is_admin,
        repo_access=repo_access,
        repo_accesses=repo_accesses,
        system_prompt_append=system_prompt_append,
        session_id=session_id,
        system_notices=system_notices,
        agent_core_module=agent_core_module,
        agent_core_class=agent_core_class,
        wrapped_on_output=wrapped_on_output,
        config_timeout=config_timeout,
        snapshot_ms=snapshot_ms,
        input_source=request.input_source,
    )


def resolved_pre_container_context(
    deps: _PreflightDeps,
    group_folder: str,
    *,
    is_admin: bool,
    repo_access_override: str | None,
) -> tuple[bool, str | None, list[str], str | None, str | None]:
    resolved = workspace_config.load_resolved_config(group_folder)
    resolved_repos = list(resolved.repo) if resolved else []
    repo_accesses = [repo_access_override] if repo_access_override is not None else resolved_repos
    repo_access = repo_accesses[0] if repo_accesses else None
    system_prompt_append = prompt_config.read_prompts(
        resolved.prompts if resolved else [],
        PersonalizationPaths.for_project(get_settings().project_root),
    )
    session_id = deps.sessions.get(group_folder)
    return is_admin, repo_access, repo_accesses, system_prompt_append, session_id


async def write_container_snapshots(
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
        [t.to_snapshot_dict() for t in tasks],
        is_admin=is_admin,
        host_jobs=[j.to_snapshot_dict() for j in host_jobs],
    )

    available_groups = await deps.get_available_groups()
    write_groups_snapshot(
        group_folder,
        available_groups,
        set(deps.workspaces.keys()),
        is_admin=is_admin,
    )
    return (time.monotonic() - snapshot_start) * 1000


def session_tracking_output_handler(
    deps: _PreflightDeps,
    group_folder: str,
    chat_jid: str,
    on_output: OnOutput | None,
) -> OnOutput:
    async def wrapped_on_output(output: ContainerOutput) -> None:
        if (
            session_id := session_id_from_output(output)
        ) and group_folder not in deps.session_cleared:
            deps.sessions[group_folder] = session_id
            await set_session(GroupFolder(group_folder), SessionId(session_id))
            # Workspace folder slugs sanitize opaque conversation IDs and are not
            # reversible. The exact durable identity belongs to the thread binding.
            if binding := await get_conversation_control_by_thread(ChatJid(chat_jid)):
                await set_conversation_session(
                    binding.conversation_id,
                    SessionId(session_id),
                )
            await update_in_flight_session(group_folder, session_id)
        if on_output:
            await on_output(output)

    return wrapped_on_output


def build_admin_system_notices(
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


def merged_system_notices(
    system_notices: list[str],
    extra_system_notices: list[str] | None,
) -> list[str]:
    if not extra_system_notices:
        return system_notices
    return [*system_notices, *extra_system_notices]
