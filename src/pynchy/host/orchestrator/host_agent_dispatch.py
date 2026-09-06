"""High-level preparation and dispatch for direct host agent turns."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from pathlib import Path  # beartype resolves annotations at runtime.
from typing import Any, Protocol, runtime_checkable

import pluggy

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
)
from pynchy.host.orchestrator._agent_runner_preflight import (
    PreContainerResult,
)
from pynchy.host.orchestrator.host_execution import (
    HostAgentTurnRequest,
    HostProcessQueue,  # beartype resolves annotations at runtime.
    HostRuntimeOperations,
    codex_thread_exists_in_host_runtime,
    host_agent_env_vars,
    prepare_host_codex_home,
    run_host_agent_turn,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.logger import logger
from pynchy.plugins.api import collect_agent_hook_specs, host_agent_hook_configs
from pynchy.state.api import clear_runtime_session_references
from pynchy.workspace.api import (
    RuntimeTarget,
    WorkspaceProfile,
)


@runtime_checkable
class BuildContainerInput(Protocol):
    """Build runner input after host session preparation is complete."""

    def __call__(  # noqa: PLR0913 - mirrors the explicit agent-execution boundary.
        self,
        messages: list[dict[str, Any]],
        ctx: PreContainerResult,
        chat_jid: str,
        group: WorkspaceProfile,
        *,
        runtime: AgentExecutionRuntime,
        is_scheduled_task: bool = False,
    ) -> ContainerInput: ...


@runtime_checkable
class HostAgentDispatchDeps(Protocol):
    """Dependencies needed to prepare one direct host invocation."""

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def plugin_manager(self) -> pluggy.PluginManager | None: ...

    @property
    def queue(self) -> HostProcessQueue: ...

    @property
    def host_runtime_operations(self) -> HostRuntimeOperations: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


async def run_host_execution(  # noqa: PLR0913 - mirrors the shared agent-runner inputs.
    deps: HostAgentDispatchDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    ctx: PreContainerResult,
    host_cwd: Path,
    build_input: BuildContainerInput,
    runtime: AgentExecutionRuntime,
    *,
    is_scheduled_task: bool,
    destroy_session: Callable[[str], Awaitable[None]],
) -> str:
    """Run one durable thread turn through the direct-host runtime."""
    operations = deps.host_runtime_operations
    codex_home = prepare_host_codex_home(group.folder, deps.plugin_manager, operations)
    session_available = codex_thread_exists_in_host_runtime(
        ctx.session_id,
        codex_home=codex_home,
    )
    if (session_id := ctx.session_id) is not None and not session_available:
        logger.info(
            "Stored Codex session is not available to host runtime; starting fresh",
            group=group.name,
            session_id=session_id,
        )
        await destroy_session(group.folder)
        await clear_runtime_session_references(
            GroupFolder(group.folder),
            SessionId(session_id),
            ChatJid(chat_jid),
        )
        deps.sessions.pop(group.folder, None)
        ctx.session_id = None
    logger.info(
        "run_agent host execution",
        group=group.name,
        cwd=str(host_cwd),
        scheduled=is_scheduled_task,
        snapshot_ms=round(ctx.snapshot_ms),
    )
    input_data = build_input(
        messages,
        ctx,
        chat_jid,
        group,
        runtime=runtime,
        is_scheduled_task=is_scheduled_task,
    )
    input_data.plugin_hooks = host_agent_hook_configs(collect_agent_hook_specs(deps.plugin_manager))
    await deps.host_runtime_operations.prepare_mcp(
        input_data,
        group.folder,
        chat_jid,
        deps.broadcast_host_message,
    )
    return await run_host_agent_turn(
        HostAgentTurnRequest(
            input_data=input_data,
            cwd=host_cwd,
            project_root=operations.project_root,
            on_output=ctx.wrapped_on_output,
            timeout_seconds=ctx.config_timeout,
            env=host_agent_env_vars(
                is_admin=ctx.is_admin,
                group_folder=group.folder,
                operations=operations,
                codex_home=codex_home,
                automation_memory_dir=(
                    Path(input_data.automation_memory_dir)
                    if input_data.automation_memory_dir is not None
                    else None
                ),
            ),
            queue=deps.queue,
            target=RuntimeTarget.from_workspace(group),
        )
    )
