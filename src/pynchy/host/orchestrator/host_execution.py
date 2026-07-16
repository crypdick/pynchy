"""Host execution helpers for direct agent runs."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlparse, urlunparse

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config import get_settings
from pynchy.host.container_manager.credentials import build_agent_env_vars
from pynchy.host.learning.mirror import prepare_full_vault_host_root
from pynchy.host.learning.paths import resolve_learning_paths
from pynchy.host.orchestrator.host_runner import run_host_input
from pynchy.logger import logger
from pynchy.types import ContainerInput, ContainerOutput

if TYPE_CHECKING:
    import asyncio

    from pynchy.host.orchestrator.queue_state import HostProcessLease

_CODEX_SESSION_PREFIX = "codex:"
HostOutput = Callable[[ContainerOutput], Awaitable[None]]


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


def codex_thread_exists_in_host_runtime(session_id: str | None) -> bool:
    thread_id = codex_thread_id(session_id)
    if thread_id is None:
        return True

    # Codex does not add every `exec` thread to session_index.jsonl, so that
    # file cannot prove whether a thread is resumable. The rollout is the
    # durable conversation state consumed by `codex exec resume`.
    sessions_path = _codex_home() / "sessions"
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


def host_agent_env_vars(*, is_admin: bool, group_folder: str) -> dict[str, str]:
    env = build_agent_env_vars(is_admin=is_admin, group_folder=group_folder)
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
    if result == "success" and has_pending_messages:
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
