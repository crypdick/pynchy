"""Pre-container setup helpers for host-side agent orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves snapshot annotations.
from typing import Any, Protocol, cast, runtime_checkable

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
    ContainerOutput,
    OnOutput,
)
from pynchy.conversation.api import new_turn_id
from pynchy.host.orchestrator.api import resolve_agent_core, resolve_container_timeout
from pynchy.host.orchestrator.conversation_control import ConversationControlClosedError
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.ipc_snapshots import write_groups_snapshot, write_tasks_snapshot
from pynchy.logger import logger
from pynchy.state.api import (
    get_all_host_jobs,
    get_all_tasks,
    get_conversation,
    get_conversation_control_by_thread,
    get_session_security_taint,
    mark_session_security_taint,
    set_conversation_session,
    set_session,
    update_in_flight_session,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
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
    post_work_prompt: str | None = None
    turn_id: str | None = None
    input_source: str = "user"
    is_scheduled_task: bool = False
    automation_memory_dir: str | None = None
    corruption_tainted: bool = False
    secret_tainted: bool = False
    agent_tool_grants: tuple[str, ...] = ()


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
    runtime: AgentExecutionRuntime
    automation_memory_dir: str | None = None


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
        query_id=new_turn_id(),
        session_id=ctx.session_id,
        group_folder=group.folder,
        chat_jid=chat_jid,
        is_admin=ctx.is_admin,
        system_notices=ctx.system_notices or None,
        is_scheduled_task=is_scheduled_task or ctx.is_scheduled_task,
        automation_memory_dir=ctx.automation_memory_dir,
        input_source=ctx.input_source,
        corruption_tainted=ctx.corruption_tainted,
        secret_tainted=ctx.secret_tainted,
        repo_access=ctx.repo_access,
        repo_accesses=ctx.repo_accesses,
        system_prompt_append=ctx.system_prompt_append,
        agent_core_module=ctx.agent_core_module,
        agent_core_class=ctx.agent_core_class,
        agent_core_config=resolved_core_config,
        agent_tool_grants=list(ctx.agent_tool_grants),
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

    def admin_repo_notices(
        self, group_folder: str, *, is_admin: bool, repo_access: str | None
    ) -> list[str]: ...


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
    if request.chat_jid.startswith("discord:") and (
        binding := await get_conversation_control_by_thread(ChatJid(request.chat_jid))
    ):
        conversation = await get_conversation(binding.conversation_id)
        if conversation.control_closed:
            raise ConversationControlClosedError(
                f"Conversation control is closed: {binding.conversation_id}"
            )
    folder = GroupFolder(request.group.folder)
    public_source = request.input_source.startswith(("webhook:", "external:"))
    secret_source = request.input_source == "external:matrix"
    if public_source or secret_source:
        taint = await mark_session_security_taint(
            folder,
            corruption_tainted=public_source,
            secret_tainted=secret_source,
        )
    else:
        taint = await get_session_security_taint(folder)
    (
        is_admin,
        repo_access,
        repo_accesses,
        system_prompt_append,
        executor_turn_context,
        session_id,
    ) = resolved_pre_container_context(
        request.deps,
        request.group.folder,
        is_admin=request.group.is_admin,
        repo_access_override=request.repo_access_override,
        input_source=request.input_source,
    )
    post_work_prompt = (
        workspace_config.read_prompts(["executors/post-work-reflection"])
        if request.is_scheduled_task
        else None
    )
    await request.deps.broadcast_agent_input(
        request.chat_jid, request.messages, source=request.input_source
    )
    snapshot_ms = await write_container_snapshots(
        request.deps,
        request.group.folder,
        is_admin=is_admin,
        data_dir=request.runtime.data_dir,
    )
    wrapped_on_output = session_tracking_output_handler(
        request.deps,
        request.group.folder,
        request.chat_jid,
        request.on_output,
    )
    access = workspace_config.load_resolved_tool_access(request.group.folder)
    system_notices = merged_system_notices(
        [
            *(
                [f"Executor context for this turn:\n{executor_turn_context}"]
                if request.is_scheduled_task
                else []
            ),
            *request.deps.admin_repo_notices(
                request.group.folder, is_admin=is_admin, repo_access=repo_access
            ),
            *(access.notices if access is not None else ()),
        ],
        request.extra_system_notices,
    )

    request.deps.session_cleared.discard(request.group.folder)
    agent_core_module, agent_core_class = resolve_agent_core(
        cast("Any", request.deps.plugin_manager), request.runtime.default_core
    )
    config_timeout = resolve_container_timeout(request.group, request.runtime.container_timeout)

    return PreContainerResult(
        is_admin=is_admin,
        repo_access=repo_access,
        repo_accesses=repo_accesses,
        system_prompt_append=system_prompt_append,
        post_work_prompt=post_work_prompt,
        session_id=session_id,
        system_notices=system_notices,
        agent_core_module=agent_core_module,
        agent_core_class=agent_core_class,
        wrapped_on_output=wrapped_on_output,
        config_timeout=config_timeout,
        snapshot_ms=snapshot_ms,
        input_source=request.input_source,
        is_scheduled_task=request.is_scheduled_task,
        automation_memory_dir=request.automation_memory_dir,
        corruption_tainted=taint.corruption_tainted,
        secret_tainted=taint.secret_tainted,
        agent_tool_grants=access.agent_tool_grants if access is not None else (),
    )


def resolved_pre_container_context(
    deps: _PreflightDeps,
    group_folder: str,
    *,
    is_admin: bool,
    repo_access_override: str | None,
    input_source: str,
) -> tuple[bool, str | None, list[str], str | None, str, str | None]:
    resolved = workspace_config.load_resolved_config(group_folder)
    resolved_repos = list(resolved.repo) if resolved else []
    repo_accesses = [repo_access_override] if repo_access_override is not None else resolved_repos
    repo_access = repo_accesses[0] if repo_accesses else None
    prompt_ids = workspace_config.prompt_ids_for_context(resolved, input_source)
    system_prompt_append = workspace_config.read_prompts(list(prompt_ids))
    executor_turn_context = workspace_config.read_prompts(list(prompt_ids[1:]))
    if executor_turn_context is None:
        raise RuntimeError("Selected executor prompt did not produce content")
    session_id = deps.sessions.get(group_folder)
    return (
        is_admin,
        repo_access,
        repo_accesses,
        system_prompt_append,
        executor_turn_context,
        session_id,
    )


def append_post_work_prompt(
    messages: list[dict[str, Any]], prompt: str | None
) -> list[dict[str, Any]]:
    """Append one resolved post-work prompt to the current user input."""
    if not prompt or not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if not isinstance(content, str):
        return messages
    return [*messages[:-1], {**last, "content": f"{content}\n\n{prompt}"}]


async def write_container_snapshots(
    deps: _PreflightDeps,
    group_folder: str,
    *,
    is_admin: bool,
    data_dir: Path,
) -> float:
    snapshot_start = time.monotonic()
    tasks = await get_all_tasks()
    host_jobs = await get_all_host_jobs() if is_admin else []
    write_tasks_snapshot(
        data_dir,
        group_folder,
        [t.to_snapshot_dict() for t in tasks],
        is_admin=is_admin,
        host_jobs=[j.to_snapshot_dict() for j in host_jobs],
    )

    available_groups = await deps.get_available_groups()
    write_groups_snapshot(
        data_dir,
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
    """Track provider sessions in the durable runtime owned by this thread."""

    async def wrapped_on_output(output: ContainerOutput) -> None:
        if output.status == "error" or (
            output.type == "tool_result" and output.tool_result_is_error is True
        ):
            # Tool output may contain secrets, so log correlation metadata only.
            logger.error(
                "Agent reported error output",
                group=group_folder,
                chat_jid=chat_jid,
                query_id=output.query_id,
                output_type=output.type,
                tool_result_id=output.tool_result_id,
            )
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
    *,
    is_admin: bool,
    repo_dirty: bool,
    unpushed_commits: int,
) -> list[str]:
    """Render admin repository notices from resolved source-control state."""
    if not is_admin:
        return []

    system_notices: list[str] = []
    if repo_dirty:
        system_notices.append(
            "There are uncommitted local changes. Run `git status` and `git diff` "
            "to review them. If they are good, commit and push. If not, discard them."
        )
    if unpushed_commits > 0:
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
