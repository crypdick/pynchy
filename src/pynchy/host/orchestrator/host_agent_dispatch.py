"""High-level preparation and dispatch for direct host agent turns."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.
from typing import Any, Protocol, runtime_checkable

import pluggy  # noqa: TC002, RUF100 - beartype resolves annotations at runtime.

from pynchy.host.container_manager import destroy_session
from pynchy.host.orchestrator._agent_runner_preflight import (
    PreContainerResult,  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
)
from pynchy.host.orchestrator.host_execution import (
    HostAgentTurnRequest,
    HostProcessQueue,  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
    codex_thread_exists_in_host_runtime,
    host_agent_env_vars,
    migrate_host_codex_thread,
    prepare_host_codex_home,
    prepare_host_direct_mcp_servers,
    run_host_agent_turn,
)
from pynchy.logger import logger
from pynchy.plugins.agent_hooks import collect_agent_hook_specs, host_agent_hook_configs
from pynchy.state import clear_session
from pynchy.types import (
    ContainerInput,  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
    GroupFolder,
    WorkspaceProfile,
)


@runtime_checkable
class BuildContainerInput(Protocol):
    """Build runner input after host session preparation is complete."""

    def __call__(
        self,
        messages: list[dict[str, Any]],
        ctx: PreContainerResult,
        chat_jid: str,
        group: WorkspaceProfile,
        *,
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

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


async def run_host_execution(  # noqa: PLR0913, RUF100 - mirrors the shared agent-runner inputs.
    deps: HostAgentDispatchDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, Any]],
    ctx: PreContainerResult,
    host_cwd: Path,
    build_input: BuildContainerInput,
    *,
    is_scheduled_task: bool,
) -> str:
    """Run an interactive or one-shot scheduled turn through the host runtime."""
    codex_home = prepare_host_codex_home(group.folder, deps.plugin_manager)
    if not is_scheduled_task:
        migrate_host_codex_thread(ctx.session_id, codex_home=codex_home)
        if not codex_thread_exists_in_host_runtime(ctx.session_id, codex_home=codex_home):
            logger.info(
                "Stored Codex session is not available to host runtime; starting fresh",
                group=group.name,
                session_id=ctx.session_id,
            )
            await destroy_session(group.folder)
            await clear_session(GroupFolder(group.folder))
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
        is_scheduled_task=is_scheduled_task,
    )
    input_data.plugin_hooks = host_agent_hook_configs(collect_agent_hook_specs(deps.plugin_manager))
    await prepare_host_direct_mcp_servers(
        input_data,
        group_folder=group.folder,
        chat_jid=chat_jid,
        broadcast_host_message=deps.broadcast_host_message,
    )
    return await run_host_agent_turn(
        HostAgentTurnRequest(
            input_data=input_data,
            cwd=host_cwd,
            on_output=ctx.wrapped_on_output,
            timeout_seconds=ctx.config_timeout,
            env=host_agent_env_vars(
                is_admin=ctx.is_admin,
                group_folder=group.folder,
                codex_home=codex_home,
            ),
            queue=deps.queue,
            chat_jid=chat_jid,
            group_folder=group.folder,
        )
    )
