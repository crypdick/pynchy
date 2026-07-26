"""Host execution helpers for direct agent runs."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from urllib.parse import urlparse, urlunparse

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config import get_settings
from pynchy.host.container_manager.credentials import build_agent_env_vars
from pynchy.host.container_manager.mcp import manager as mcp_manager
from pynchy.host.container_manager.security.gate import create_gate, resolve_security
from pynchy.host.learning.mirror import prepare_full_vault_host_root
from pynchy.host.learning.paths import resolve_learning_paths
from pynchy.host.learning.skill_activation import prepare_agent_homes
from pynchy.host.orchestrator.host_runner import run_host_input
from pynchy.host.orchestrator.mcp_notifications import notify_mcp_startup_failures
from pynchy.logger import logger
from pynchy.types import ContainerInput, ContainerOutput

if TYPE_CHECKING:
    import asyncio

    import pluggy

    from pynchy.host.orchestrator.queue_state import HostProcessLease

_CODEX_SESSION_PREFIX = "codex:"
HostOutput = Callable[[ContainerOutput], Awaitable[None]]


@runtime_checkable
class HostProcessQueue(Protocol):
    """Queue operations that bridge a direct Temporal host process."""

    def acquire_host_process(self, group_jid: str) -> HostProcessLease: ...

    def register_host_process(  # noqa: PLR0913, RUF100 - mirrors GroupQueue's process-registration contract.
        self,
        lease: HostProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str | None = None,
        invocation_ts: float = 0.0,
    ) -> bool: ...

    def boundary_interrupt_requested(self, group_jid: str) -> bool: ...

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
    chat_jid: str
    group_folder: str


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

    # Codex does not add every `exec` thread to session_index.jsonl, so that
    # file cannot prove whether a thread is resumable. The rollout is the
    # durable conversation state consumed by `codex exec resume`.
    sessions_path = (codex_home or _codex_home()) / "sessions"
    expected_suffix = f"-{thread_id}.jsonl"
    try:
        return any(
            path.name.startswith("rollout-") and path.name.endswith(expected_suffix)
            for path in sessions_path.rglob("*.jsonl")
        )
    except OSError:
        logger.warning("Could not inspect Codex rollout sessions", path=str(sessions_path))
        return False


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

    source_home = legacy_codex_home or _codex_home()
    source_sessions = source_home / "sessions"
    expected_suffix = f"-{thread_id}.jsonl"
    try:
        rollout = next(
            (
                path
                for path in source_sessions.rglob("*.jsonl")
                if path.name.startswith("rollout-") and path.name.endswith(expected_suffix)
            ),
            None,
        )
    except OSError:
        logger.warning("Could not inspect legacy Codex rollouts", path=str(source_sessions))
        return False
    if rollout is None:
        return False

    destination = codex_home / "sessions" / rollout.relative_to(source_sessions)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(rollout, destination)
    except OSError:
        logger.warning(
            "Could not scope legacy Codex rollout",
            source=str(rollout),
            destination=str(destination),
        )
        return False
    logger.info("Scoped legacy Codex rollout", thread_id=thread_id, destination=str(destination))
    return True


def host_agent_env_vars(
    *, is_admin: bool, group_folder: str, codex_home: Path | None = None
) -> dict[str, str]:
    env = build_agent_env_vars(is_admin=is_admin, group_folder=group_folder)
    s = get_settings()
    # Direct-host CLI hooks are fresh subprocesses, separate from the Pynchy MCP
    # process configured in host_direct. They therefore need the workspace
    # identity in their inherited environment as well as the group-scoped IPC path.
    env["PYNCHY_GROUP_FOLDER"] = group_folder
    env["PYNCHY_IS_ADMIN"] = "1" if is_admin else "0"
    env["PYNCHY_IPC_DIR"] = str(s.data_dir / "ipc" / group_folder)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    for key in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        if key in env:
            env[key] = _host_reachable_gateway_url(env[key])
    if (
        is_admin
        and (learning_paths := resolve_learning_paths(group_folder)) is not None
        and (host_vault_root := prepare_full_vault_host_root(learning_paths))
    ):
        env["OBSIDIAN_VAULT_PATH"] = str(host_vault_root)
        env["PYNCHY_SKILLS_ROOT"] = str(host_vault_root / "systems" / "pynchy" / "skills")
    return env


async def prepare_host_direct_mcp_servers(
    input_data: ContainerInput,
    *,
    group_folder: str,
    chat_jid: str,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
) -> None:
    """Register host security context, then attach selected MCP proxy routes."""
    invocation_ts = time.monotonic()
    security = resolve_security(group_folder, is_admin=input_data.is_admin)
    create_gate(
        group_folder,
        invocation_ts,
        security,
        public_source_input=input_data.corruption_tainted,
        secret_source_input=input_data.secret_tainted,
    )
    input_data.invocation_ts = invocation_ts

    mcp_mgr = mcp_manager.get_mcp_manager()
    if mcp_mgr is None:
        return

    mcp_startup = await mcp_mgr.ensure_workspace_running(group_folder)
    if mcp_startup.failures:
        await notify_mcp_startup_failures(
            broadcast_host_message,
            chat_jid,
            mcp_startup.failures,
        )
    input_data.mcp_direct_servers = mcp_mgr.get_direct_server_configs(
        group_folder,
        invocation_ts=input_data.invocation_ts,
        instance_ids=mcp_startup.ready_instance_ids,
    )


async def run_host_agent_turn(request: HostAgentTurnRequest) -> str:
    """Run a host turn while exposing its process to inbound message routing."""
    lease = request.queue.acquire_host_process(request.chat_jid)

    def register_spawned_process(proc: asyncio.subprocess.Process) -> None:
        if not request.queue.register_host_process(
            lease,
            proc,
            "host-agent-runner",
            request.group_folder,
            request.input_data.invocation_ts,
        ):
            raise RuntimeError("Host process lease expired before process registration")

    try:
        result = await run_host_input(
            request.input_data,
            cwd=request.cwd,
            on_output=request.on_output,
            timeout_seconds=request.timeout_seconds,
            env=request.env,
            on_process_started=lambda proc: register_spawned_process(
                cast("asyncio.subprocess.Process", proc)
            ),
            is_interrupted=lambda: request.queue.boundary_interrupt_requested(request.chat_jid),
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
