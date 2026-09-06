"""Persistent container sessions with owned process monitoring and stderr capture.

The IPC watcher routes output files to the active query callback and signals
completion. A CLI process can exit before its runtime container, so runtime
probes determine when the session has actually ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
)
from dataclasses import dataclass
from pathlib import (
    Path,
)
from typing import Any

import pluggy

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
    McpStartupFailure,
    OnOutput,
)
from pynchy.async_tasks import create_background_task
from pynchy.host.container_manager.ipc.write import (
    clean_ipc_input_dir,
    clean_secret_files,
    write_ipc_close_sentinel,
    write_ipc_message,
)
from pynchy.host.container_manager.orchestrator import _spawn_container, stable_container_name
from pynchy.host.container_manager.process import (
    docker_rm_force,
    graceful_stop,
    reap_apple_runtime_orphans,
    runtime_container_running,
)
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate, resolve_security
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.logger import logger
from pynchy.progress_wait import ProgressTimeoutError, wait_for_progress
from pynchy.workspace.api import WorkspaceProfile


class SessionDiedError(Exception):
    """Raised when the container process exits unexpectedly."""


_MISSING_STDERR_PIPE_ERROR = "Container {container_name} spawned without stderr pipe"
_CONTAINER_DIED_DURING_QUERY_ERROR = "Container {container_name} died during query"


@dataclass(frozen=True)
class RuntimeMonitorPolicy:
    """Timing policy for checking a container runtime outside its CLI process."""

    poll_interval_seconds: float = 0.5
    start_grace_seconds: float = 5.0
    cli_kill_wait_seconds: float = 2.0


DEFAULT_RUNTIME_MONITOR_POLICY = RuntimeMonitorPolicy()


class ContainerSession:
    """Own one container's lifetime, query callbacks, and idle expiry."""

    def __init__(
        self,
        group_folder: str,
        container_name: str,
        *,
        invocation_ts: float = 0.0,
        runtime_probe: Callable[[str], Awaitable[bool]] = runtime_container_running,
        runtime_monitor_policy: RuntimeMonitorPolicy = DEFAULT_RUNTIME_MONITOR_POLICY,
    ) -> None:
        self.group_folder = group_folder
        self.container_name = container_name
        self.invocation_ts = invocation_ts
        self.proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._on_output: OnOutput | None = None
        self._active_query_id: str | None = None
        self._query_done = asyncio.Event()
        self._query_progress = asyncio.Event()
        self._dead = False
        self._died_before_pulse = False
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_timeout = 0.0
        self._on_idle_expire: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._runtime_probe = runtime_probe
        self._runtime_monitor_policy = runtime_monitor_policy

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and not self._dead

    @property
    def output_handler(self) -> OnOutput | None:
        """Return the callback currently receiving output for this session."""
        return self._on_output

    def set_idle_timeout(self, timeout_seconds: float) -> None:
        """Set the idle timeout used when this session returns to idle."""
        self._idle_timeout = timeout_seconds

    def start(self, proc: asyncio.subprocess.Process) -> None:
        """Attach the process and start its lifetime monitor and stderr reader."""
        if proc.stderr is None:
            raise RuntimeError(
                _MISSING_STDERR_PIPE_ERROR.format(container_name=self.container_name)
            )
        self.proc = proc
        self._dead = False
        self._tasks = [
            create_background_task(
                self._read_stderr(proc.stderr), name=f"stderr-{self.container_name}"
            ),
            create_background_task(
                self._monitor_proc(proc), name=f"proc-monitor-{self.container_name}"
            ),
        ]
        self._reset_idle_timer()

    def set_output_handler(
        self,
        on_output: OnOutput | None,
        *,
        query_id: str | None = None,
    ) -> None:
        """Start one query generation with its output callback and identity."""
        self._on_output = on_output
        self._active_query_id = query_id
        self._query_done.clear()
        self._query_progress.clear()
        self._died_before_pulse = False
        self._cancel_idle_timer()

    def signal_query_progress(self, query_id: str | None) -> bool:
        """Refresh the silence deadline for output from the active query only."""
        if self._on_output is None or self._active_query_id != query_id:
            return False
        self._query_progress.set()
        return True

    def signal_query_done(self, query_id: str | None = None) -> bool:
        """Signal that the current query is complete.

        Called by the IPC watcher when it detects a query-done pulse in an
        output file.  Sets the _query_done event, clears the output handler,
        and resets the idle timer.
        """
        if self._on_output is not None and self._active_query_id != query_id:
            return False
        self._query_done.set()
        self._query_progress.set()
        self._on_output = None
        self._active_query_id = None
        self._reset_idle_timer()
        return True

    async def send_ipc_message(
        self,
        text: str,
        *,
        turn_id: str | None = None,
        query_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write a JSON message file to the container's IPC input directory."""
        write_ipc_message(
            self.group_folder,
            text,
            turn_id=turn_id,
            query_id=query_id,
            metadata=metadata,
        )

    async def wait_for_query_done(self, query_timeout_seconds: float) -> None:
        """Wait for completion while structured output refreshes the deadline.

        ``query_timeout_seconds`` is the maximum silence between progress
        events. A separate four-times hard ceiling bounds noisy wedges.

        Raises TimeoutError if either deadline expires.
        Raises SessionDiedError if the container exits *before* the pulse.

        If the pulse is detected and then the container exits (EOF after pulse),
        this is not an error — the pulse already confirmed query completion.
        """
        try:
            await wait_for_progress(
                self._query_done.wait(),
                progress_event=self._query_progress,
                inactivity_timeout_seconds=query_timeout_seconds,
            )
        except ProgressTimeoutError as exc:
            logger.error(
                "Session query timed out",
                group=self.group_folder,
                container=self.container_name,
                query_timeout_seconds=query_timeout_seconds,
                timeout_reason=exc.reason,
                hard_timeout_seconds=exc.hard_timeout_seconds,
            )
            raise

        if self._died_before_pulse:
            raise SessionDiedError(
                _CONTAINER_DIED_DURING_QUERY_ERROR.format(container_name=self.container_name)
            )

    async def stop(self) -> None:
        """Stop the container and retire every resource even if cleanup fails."""
        self._cancel_idle_timer()
        self._dead = True
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.callback(self._finalize_exit)
            cleanup.push_async_callback(self._cancel_background_tasks)
            cleanup.push_async_callback(docker_rm_force, self.container_name)
            with contextlib.suppress(OSError):
                write_ipc_close_sentinel(self.group_folder)
            if self.proc and self.proc.returncode is None:
                await graceful_stop(self.proc, self.container_name)

    async def _cancel_background_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _finalize_exit(self) -> None:
        # Retain the security gate while IPC drains; always release query waiters.
        self._cancel_idle_timer()
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(self._query_progress.set)
            cleanup.callback(self._query_done.set)
            cleanup.callback(clean_secret_files, self.group_folder)
            self._destroy_security_gate()

    def _reset_idle_timer(self) -> None:
        """Start or restart the idle expiry timer."""
        self._cancel_idle_timer()
        if self._idle_timeout > 0:
            loop = asyncio.get_running_loop()
            self._idle_handle = loop.call_later(self._idle_timeout, self._on_idle_expired)

    def _cancel_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def set_idle_callback(self, callback: Callable[[], Coroutine[Any, Any, None]] | None) -> None:
        """Register a callback to run when the idle timer expires.

        Used by the pipeline to send the zzz reaction when the container
        actually hibernates, rather than when the query finishes.
        """
        self._on_idle_expire = callback

    def _on_idle_expired(self) -> None:
        """Called when the session exceeds the idle timeout."""
        expired_handle = self._idle_handle
        logger.info(
            "Session idle timeout, destroying",
            group=self.group_folder,
            container=self.container_name,
        )

        async def _idle_teardown() -> None:
            if self._idle_handle is not expired_handle:
                return
            if self._on_idle_expire is not None:
                try:
                    await self._on_idle_expire()
                except Exception:  # noqa: BLE001 - idle callback is best-effort teardown and must not block destroy.
                    logger.exception(
                        "Idle callback failed",
                        group=self.group_folder,
                    )
            # Query reuse or shutdown invalidates this timer while we await.
            if self._idle_handle is expired_handle:
                await destroy_session(self.group_folder)

        create_background_task(
            _idle_teardown(),
            name=f"idle-destroy-{self.group_folder}",
        )

    async def _monitor_proc(self, proc: asyncio.subprocess.Process) -> None:
        """Use one observer for CLI exit and actual runtime container death.

        Docker's CLI normally tracks container lifetime. Apple Container can
        leave either the CLI or the VM running after the other has stopped.
        """
        if sys.platform == "darwin":
            seen_running = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._runtime_monitor_policy.start_grace_seconds
            while proc.returncode is None and not self._dead:
                running = await self._runtime_probe(self.container_name)
                # A probe can finish after CLI exit or shutdown; re-check ownership.
                if proc.returncode is not None or self._dead:
                    break
                if running:
                    seen_running = True
                elif seen_running or loop.time() >= deadline:
                    logger.warning(
                        "Runtime container unavailable while CLI process remained alive",
                        group=self.group_folder,
                        container=self.container_name,
                        started=seen_running,
                    )
                    await self._kill_stuck_runtime_cli(proc)
                    return
                await asyncio.sleep(self._runtime_monitor_policy.poll_interval_seconds)

        exit_code = await proc.wait()
        if self._dead:
            return
        if await self._runtime_probe(self.container_name):
            logger.info(
                "Container CLI process exited while runtime container remains running",
                group=self.group_folder,
                container=self.container_name,
                exit_code=exit_code,
            )
            while await self._runtime_probe(self.container_name):  # noqa: ASYNC110 - the detached runtime only exposes polling.
                await asyncio.sleep(self._runtime_monitor_policy.poll_interval_seconds)
        self._mark_container_exited(exit_code)

    async def _kill_stuck_runtime_cli(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                proc.wait(), timeout=self._runtime_monitor_policy.cli_kill_wait_seconds
            )
        await reap_apple_runtime_orphans(self.container_name)
        self._mark_container_exited(1)

    def _mark_container_exited(self, exit_code: int) -> None:
        """Classify exit against the query pulse before retiring session resources."""
        was_stopping = self._dead
        self._dead = True
        if not was_stopping and not self._query_done.is_set():
            # A clean exit without a pulse represents intentional reset_context.
            self._died_before_pulse = exit_code != 0
        logger.info(
            "Session proc exited",
            group=self.group_folder,
            container=self.container_name,
            exit_code=exit_code,
        )
        if not was_stopping:
            create_background_task(
                docker_rm_force(self.container_name),
                name=f"remove-exited-container-{self.container_name}",
            )
        self._finalize_exit()

    def _destroy_security_gate(self) -> None:
        """Retire the gate with the worker process that owns it."""
        if not self.invocation_ts:
            return
        destroy_gate(self.group_folder, self.invocation_ts)
        self.invocation_ts = 0.0

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        """Long-lived stderr reader — logs container stderr lines."""
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            text = chunk.decode(errors="replace")
            for line in text.strip().splitlines():
                if line:
                    logger.debug(line, container=self.group_folder)


# ---------------------------------------------------------------------------
# Module-level session registry
# ---------------------------------------------------------------------------

_sessions: dict[str, ContainerSession] = {}


def get_session(group_folder: GroupFolder) -> ContainerSession | None:
    """Return the alive session for a group, or None.

    Cleans up dead sessions on access.
    """
    session = _sessions.get(group_folder)
    if session is None:
        return None
    if not session.is_alive:
        logger.info(
            "Cleaning up dead session",
            group=group_folder,
            container=session.container_name,
        )
        _sessions.pop(group_folder, None)
        return None
    return session


def active_session_container_names() -> set[str]:
    """Return container names owned by live in-process sessions."""
    return {session.container_name for session in _sessions.values() if session.is_alive}


def active_session_group_folders() -> set[str]:
    """Return group folders owned by live in-process sessions."""
    return {folder for folder, session in _sessions.items() if session.is_alive}


def get_session_output_handler(group_folder: GroupFolder) -> OnOutput | None:
    """Return the output handler for the active session of a group, or None.

    Used by the IPC watcher to dispatch output events to the correct callback.
    Returns None if no session is active or no handler is set.
    """
    session = get_session(group_folder)
    if session is None:
        return None
    return session.output_handler


async def start_session(
    group: WorkspaceProfile,
    input_data: ContainerInput,
    runtime: AgentExecutionRuntime,
    plugin_manager: pluggy.PluginManager | None = None,
) -> tuple[ContainerSession, tuple[McpStartupFailure, ...]]:
    """Retire stale resources, then spawn and register one owned worker.

    IPC cleanup precedes spawning so it cannot delete the worker's first output.
    Rollback owns security and process cleanup until registration succeeds.
    """
    await destroy_session(group.folder)
    container_name = stable_container_name(group.folder)
    await docker_rm_force(container_name)
    clean_ipc_input_dir(group.folder)
    _clean_ipc_output(runtime.data_dir, group.folder)

    session = ContainerSession(group.folder, container_name, invocation_ts=time.monotonic())
    input_data.invocation_ts = session.invocation_ts
    session.set_idle_timeout(runtime.idle_timeout)
    async with contextlib.AsyncExitStack() as rollback:
        rollback.push_async_callback(session.stop)
        create_gate(
            group.folder,
            session.invocation_ts,
            resolve_security(group.folder, is_admin=input_data.is_admin),
            public_source_input=input_data.corruption_tainted,
            secret_source_input=input_data.secret_tainted,
        )
        proc, failures = await _spawn_container(
            group, input_data, container_name, runtime, plugin_manager
        )
        # Retain the process for rollback even if attaching its readers fails.
        session.proc = proc
        session.start(proc)
        _sessions[group.folder] = session
        rollback.pop_all()

    logger.info("Session created", group=group.folder, container=container_name)
    return session, failures


async def destroy_session(group_folder: str) -> None:
    """Stop and remove the session for a group."""
    session = _sessions.pop(group_folder, None)
    if session is None:
        return
    await session.stop()
    logger.info(
        "Session destroyed",
        group=group_folder,
        container=session.container_name,
    )


async def destroy_all_sessions() -> None:
    """Stop all sessions — called during shutdown."""
    folders = list(_sessions.keys())
    if not folders:
        return
    logger.info("Destroying all sessions", count=len(folders))
    await asyncio.gather(
        *(destroy_session(f) for f in folders),
        return_exceptions=True,
    )


def _clean_ipc_output(data_dir: Path, group_folder: str) -> None:
    """Remove stale IPC output files for a group.

    Called when creating a session to prevent replay of output events
    left by a dead session.  Output files are ephemeral mid-query
    artefacts — they have no value once the session that produced them is
    gone.
    """
    output_dir = data_dir / "ipc" / group_folder / "output"
    if not output_dir.is_dir():
        return
    for f in output_dir.iterdir():
        with contextlib.suppress(OSError):
            f.unlink()
