"""Host execution helpers for direct agent runs."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from urllib.parse import urlparse, urlunparse

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config.api import get_settings
from pynchy.host.learning.api import (
    prepare_agent_homes,
    prepare_full_vault_host_root,
    resolve_learning_paths,
)
from pynchy.host.orchestrator.codex_rollouts import (
    CodexRolloutInspectionError as _CodexRolloutInspectionError,
)
from pynchy.host.orchestrator.codex_rollouts import (
    migrate_rollout,
    rollout_exists,
)
from pynchy.host.orchestrator.host_runner import run_host_input
from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR, SKILLS_DIRNAME
from pynchy.types import (  # noqa: TC001, RUF100
    ContainerInput,
    ContainerOutput,
    RuntimeId,
    RuntimeTarget,  # noqa: TC001, RUF100
)

if TYPE_CHECKING:
    import asyncio

    import pluggy

    from pynchy.host.orchestrator.queue_state import HostProcessLease
_CODEX_SESSION_PREFIX = "codex:"
CodexRolloutInspectionError = _CodexRolloutInspectionError
HostOutput = Callable[[ContainerOutput], Awaitable[None]]


@dataclass
class HostRuntimeOperations:
    """Host-runtime capabilities selected by the application composition root."""

    build_agent_environment: Callable[..., dict[str, str]]
    prepare_mcp: Callable[..., Awaitable[None]]


@runtime_checkable
class HostProcessQueue(Protocol):
    """Queue operations that bridge a direct Temporal host process."""

    def acquire_host_process(self, target: RuntimeTarget) -> HostProcessLease: ...

    def register_host_process(  # noqa: PLR0913, RUF100 - mirrors GroupQueue's process-registration contract.
        self,
        lease: HostProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float = 0.0,
    ) -> bool: ...

    def boundary_interrupt_requested(self, runtime_id: RuntimeId) -> bool: ...

    def release_host_process(self, lease: HostProcessLease) -> bool: ...


@dataclass(frozen=True)
class HostAgentTurnRequest:
    """Inputs required to run and track one direct host agent turn."""

    input_data: ContainerInput
    cwd: Path
    on_output: HostOutput
    timeout_seconds: int | float
    env: dict[str, str]
    queue: HostProcessQueue
    target: RuntimeTarget


def codex_thread_id(session_id: str | None) -> str | None:
    if not session_id or not session_id.startswith(_CODEX_SESSION_PREFIX):
        return None
    body = session_id.removeprefix(_CODEX_SESSION_PREFIX)
    if ":" in body:
        _model, body = body.split(":", maxsplit=1)
    return body or None


def codex_thread_exists_in_host_runtime(
    session_id: str | None, *, codex_home: Path | None = None
) -> bool:
    thread_id = codex_thread_id(session_id)
    if thread_id is None:
        return True

    # session_index.jsonl is incomplete for `exec` threads; only the exact
    # rollout header proves that `codex exec resume` has durable state.
    return rollout_exists(codex_home or _codex_home(), thread_id)


def host_execution_cwd(group_folder: str) -> Path | None:
    resolved = workspace_config.load_resolved_config(group_folder)
    if resolved is None or resolved.execution_mode != "host":
        return None
    if not resolved.cwd:
        return None
    return Path(resolved.cwd).expanduser()


def host_codex_home(group_folder: str) -> Path:
    """Return the isolated Codex home for one direct-host workspace."""
    return get_settings().data_dir / "sessions" / group_folder / ".codex"


def prepare_host_codex_home(group_folder: str, plugin_manager: pluggy.PluginManager | None) -> Path:
    """Synchronize selected skills into a direct-host workspace's Codex home."""
    return prepare_agent_homes(group_folder, plugin_manager).codex_home


def migrate_host_codex_thread(
    session_id: str | None,
    *,
    codex_home: Path,
    legacy_codex_home: Path | None = None,
) -> bool:
    """Copy a pre-scoped host rollout into its workspace-local Codex home."""
    thread_id = codex_thread_id(session_id)
    if thread_id is None:
        return True
    if codex_thread_exists_in_host_runtime(session_id, codex_home=codex_home):
        return True

    return (
        migrate_rollout(
            thread_id,
            codex_home=codex_home,
            legacy_codex_home=legacy_codex_home or _codex_home(),
            scoped_sessions_root=get_settings().data_dir / "sessions",
        )
        is not None
    )


def host_agent_env_vars(
    *,
    is_admin: bool,
    group_folder: str,
    build_agent_environment: Callable[..., dict[str, str]],
    codex_home: Path | None = None,
) -> dict[str, str]:
    env = build_agent_environment(
        is_admin=is_admin,
        group_folder=group_folder,
    )
    s = get_settings()
    # Direct-host CLI hooks are fresh subprocesses, separate from the Pynchy MCP
    # process configured in host_direct. They therefore need the workspace
    # identity in their inherited environment as well as the group-scoped IPC path.
    env["PYNCHY_GROUP_FOLDER"] = group_folder
    env["PYNCHY_IS_ADMIN"] = "1" if is_admin else "0"
    env["PYNCHY_IPC_DIR"] = str(s.data_dir / "ipc" / group_folder)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    personalization_skills = s.project_root / PERSONALIZATION_RELATIVE_DIR / SKILLS_DIRNAME
    personalization_skills.mkdir(parents=True, exist_ok=True)
    env["PYNCHY_SKILLS_ROOT"] = str(personalization_skills)
    # Host-direct agents must be able to edit this intentionally shared source
    # without the parent repository's target-branch guard treating it as code.
    ceilings = [str(personalization_skills)]
    if existing_ceiling := env.get("GIT_CEILING_DIRECTORIES"):
        ceilings.append(existing_ceiling)
    env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(ceilings)
    for key in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        if key in env:
            env[key] = _host_reachable_gateway_url(env[key])
    if (
        is_admin
        and (learning_paths := resolve_learning_paths(group_folder)) is not None
        and (host_vault_root := prepare_full_vault_host_root(learning_paths))
    ):
        env["OBSIDIAN_VAULT_PATH"] = str(host_vault_root)
    return env


async def run_host_agent_turn(request: HostAgentTurnRequest) -> str:
    """Run a host turn while exposing its process to inbound message routing."""
    lease = request.queue.acquire_host_process(request.target)

    def register_spawned_process(proc: asyncio.subprocess.Process) -> bool:
        return bool(
            request.queue.register_host_process(
                lease,
                proc,
                "host-agent-runner",
                request.input_data.invocation_ts,
            )
        )

    try:
        result = await run_host_input(
            request.input_data,
            cwd=request.cwd,
            project_root=get_settings().project_root,
            on_output=request.on_output,
            timeout_seconds=request.timeout_seconds,
            env=request.env,
            on_process_started=lambda proc: register_spawned_process(
                cast("asyncio.subprocess.Process", proc)
            ),
            is_interrupted=lambda: request.queue.boundary_interrupt_requested(request.target.id),
        )
    except BaseException:
        request.queue.release_host_process(lease)
        raise

    has_pending_messages = request.queue.release_host_process(lease) is True
    if result in {"success", "interrupted"} and has_pending_messages:
        return "success_with_pending_input"
    return result


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _host_reachable_gateway_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    port = parsed.port or get_settings().gateway.port
    return urlunparse(parsed._replace(netloc=f"localhost:{port}"))
