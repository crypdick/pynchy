"""Host execution helpers for direct agent runs."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from urllib.parse import urlparse, urlunparse

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.agent_protocol.api import (
    ContainerInput,
    ContainerOutput,
)
from pynchy.conversation.api import conversation_id_from_folder
from pynchy.host.orchestrator.codex_rollouts import (
    CodexRolloutInspectionError as _CodexRolloutInspectionError,
)
from pynchy.host.orchestrator.codex_rollouts import prepare_rollout_resume
from pynchy.host.orchestrator.host_runner import run_host_input
from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR, SKILLS_DIRNAME
from pynchy.identifiers import RuntimeId  # noqa: TC001
from pynchy.workspace.api import RuntimeTarget  # noqa: TC001

if TYPE_CHECKING:
    import asyncio

    from pynchy.host.orchestrator.queue_state import HostProcessLease
_CODEX_SESSION_PREFIX = "codex:"
CodexRolloutInspectionError = _CodexRolloutInspectionError
HostOutput = Callable[[ContainerOutput], Awaitable[None]]


class HostExecutionCwdError(RuntimeError):
    """The direct-host process cannot start from a safe working directory."""


@dataclass(frozen=True)
class HostExecutionCwd:
    """Prepared direct-host working directory and agent-facing notices."""

    path: Path
    notices: tuple[str, ...] = ()
    repo_access: str | None = None


@dataclass(frozen=True)
class RoutedHostRoute:
    """Host-owned source repository binding for one routed host turn."""

    repo_access: str
    turn_id: str


_active_routed_host_repos: dict[str, RoutedHostRoute] = {}


def bind_active_routed_host_repo(group_folder: str, repo_access: str, turn_id: str) -> None:
    """Associate an active routed host turn with its repository and turn identity."""
    _active_routed_host_repos[group_folder] = RoutedHostRoute(repo_access, turn_id)


def clear_active_routed_host_repo(group_folder: str, repo_access: str, turn_id: str) -> None:
    """Remove a routed host repository association when its turn completes."""
    if _active_routed_host_repos.get(group_folder) == RoutedHostRoute(repo_access, turn_id):
        _active_routed_host_repos.pop(group_folder, None)


def active_routed_host_repo(group_folder: str) -> RoutedHostRoute | None:
    """Return the host-owned identity for an active routed host turn."""
    return _active_routed_host_repos.get(group_folder)


class RoutedHostCwdResolver(Protocol):
    """Composition-owned resolver for a routed host conversation's source tree."""

    def __call__(
        self,
        group_folder: str,
        source_cwd: Path,
        repo_accesses: Sequence[str],
        *,
        recovered: bool,
    ) -> HostExecutionCwd: ...


@dataclass
class HostRuntimeOperations:
    """Host-runtime capabilities selected by the application composition root."""

    build_agent_environment: Callable[..., dict[str, str]]
    prepare_mcp: Callable[..., Awaitable[None]]
    sessions_root: Path
    project_root: Path
    gateway_port: int
    prepare_host_codex_home: Callable[[str, object | None], Path]
    host_learning_vault: Callable[[str], Path | None]
    resolve_routed_host_cwd: RoutedHostCwdResolver


@runtime_checkable
class HostProcessQueue(Protocol):
    """Queue operations that bridge a direct Temporal host process."""

    def acquire_host_process(self, target: RuntimeTarget) -> HostProcessLease: ...

    def register_host_process(  # mirrors GroupQueue's process-registration contract.
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
    project_root: Path
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
    return prepare_rollout_resume(codex_home or _codex_home(), thread_id)


def host_execution_cwd(
    group_folder: str,
    operations: HostRuntimeOperations,
    *,
    repo_accesses: Sequence[str],
    recovered: bool,
) -> HostExecutionCwd | None:
    """Prepare host CWD, isolating only stable repository-backed routed conversations."""
    resolved = workspace_config.load_resolved_config(group_folder)
    if resolved is None:
        if conversation_id_from_folder(group_folder) is not None:
            raise HostExecutionCwdError(
                "Routed conversation workspace policy is unavailable; refusing container fallback."
            )
        return None
    if resolved.execution_mode != "host":
        return None
    if not resolved.cwd:
        return None
    source_cwd = Path(resolved.cwd).expanduser()
    if conversation_id_from_folder(group_folder) is None or not repo_accesses:
        return HostExecutionCwd(source_cwd)
    return operations.resolve_routed_host_cwd(
        group_folder,
        source_cwd,
        repo_accesses,
        recovered=recovered,
    )


def prepare_host_codex_home(
    group_folder: str,
    plugin_manager: object | None,
    operations: HostRuntimeOperations,
) -> Path:
    """Synchronize selected skills into a direct-host workspace's Codex home."""
    return operations.prepare_host_codex_home(group_folder, plugin_manager)


def host_agent_env_vars(
    *,
    is_admin: bool,
    group_folder: str,
    operations: HostRuntimeOperations,
    codex_home: Path | None = None,
    automation_memory_dir: Path | None = None,
) -> dict[str, str]:
    env = operations.build_agent_environment(
        is_admin=is_admin,
        group_folder=group_folder,
    )
    # Direct-host CLI hooks are fresh subprocesses, separate from the Pynchy MCP
    # process configured in host_direct. They therefore need the workspace
    # identity in their inherited environment as well as the group-scoped IPC path.
    env["PYNCHY_GROUP_FOLDER"] = group_folder
    env["PYNCHY_IS_ADMIN"] = "1" if is_admin else "0"
    env["PYNCHY_IPC_DIR"] = str(operations.sessions_root.parent / "ipc" / group_folder)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    if automation_memory_dir is not None:
        env["PYNCHY_AUTOMATION_MEMORY_DIR"] = str(automation_memory_dir)
    personalization_skills = operations.project_root / PERSONALIZATION_RELATIVE_DIR / SKILLS_DIRNAME
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
            env[key] = _host_reachable_gateway_url(env[key], operations.gateway_port)
    if is_admin and (host_vault_root := operations.host_learning_vault(group_folder)) is not None:
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
        return await run_host_input(
            request.input_data,
            cwd=request.cwd,
            project_root=request.project_root,
            on_output=request.on_output,
            timeout_seconds=request.timeout_seconds,
            env=request.env,
            on_process_started=lambda proc: register_spawned_process(
                cast("asyncio.subprocess.Process", proc)
            ),
            is_interrupted=lambda: request.queue.boundary_interrupt_requested(request.target.id),
        )
    finally:
        request.queue.release_host_process(lease)


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _host_reachable_gateway_url(base_url: str, gateway_port: int) -> str:
    parsed = urlparse(base_url)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    port = parsed.port or gateway_port
    return urlunparse(parsed._replace(netloc=f"localhost:{port}"))
