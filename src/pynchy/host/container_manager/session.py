"""Persistent container sessions — keep containers alive between message rounds.

A ContainerSession owns a running container process and its I/O readers.
Sessions live in a module-level registry keyed by group_folder.  The session
provides methods to send IPC messages and wait for query completion, decoupling
container lifecycle from individual message processing.

Two paths through run_agent():
  Cold path: first message or after reset — spawn container, start readers
  Warm path: subsequent messages — send IPC message, wait for query done

Output routing:
  Output arrives as files in the IPC output/ directory and is processed by the
  IPC watcher (_watcher.py).  The watcher calls get_session_output_handler() to
  look up the current callback and signal_query_done() when a query-done pulse
  is detected.  The session reads only stderr (for log capture) and monitors
  proc.wait() for unexpected death.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import (  # noqa: TC003 - beartype resolves session callback signatures at runtime.
    Awaitable,
    Callable,
    Coroutine,
)
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves session path annotations at runtime.
)
from typing import Any

from pynchy.agent_protocol.api import (
    OnOutput,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.async_tasks import create_background_task
from pynchy.host.container_manager.ipc.write import (
    clean_ipc_input_dir,
    write_ipc_close_sentinel,
    write_ipc_message,
)
from pynchy.host.container_manager.process import (
    docker_rm_force,
    graceful_stop,
    reap_apple_runtime_orphans,
    runtime_container_running,
)
from pynchy.host.container_manager.security.gate import destroy_gate
from pynchy.identifiers import (
    GroupFolder,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.logger import logger
from pynchy.progress_wait import ProgressTimeoutError, wait_for_progress


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


async def _wait_for_runtime_poll_interval(interval_seconds: float) -> None:
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    handle = loop.call_later(interval_seconds, waiter.set_result, None)
    try:
        await waiter
    finally:
        handle.cancel()


class ContainerSession:
    """Owns a running container process and provides query-level interaction.

    Output is routed through file-based IPC: the container writes output files,
    the host IPC watcher processes them and calls back into the session via
    signal_query_done() and get_session_output_handler().

    The session monitors stderr (for log capture) and proc.wait() (for
    detecting unexpected container death).
    """

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
        self._proc_monitor_task: asyncio.Future[Any] | None = None
        self._runtime_monitor_task: asyncio.Future[Any] | None = None
        self._stderr_task: asyncio.Future[Any] | None = None
        self._on_output: OnOutput | None = None
        self._active_query_id: str | None = None
        self._query_done = asyncio.Event()
        self._query_progress = asyncio.Event()
        self._dead = False
        self._died_before_pulse = False
        self._runtime_alive_after_proc_exit = False
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_timeout = 0.0
        self._on_idle_expire: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._runtime_probe = runtime_probe
        self._runtime_monitor_policy = runtime_monitor_policy

    @property
    def is_alive(self) -> bool:
        if self.proc is None or self._dead:
            return False
        return self.proc.returncode is None or self._runtime_alive_after_proc_exit

    @property
    def output_handler(self) -> OnOutput | None:
        """Return the callback currently receiving output for this session."""
        return self._on_output

    def set_idle_timeout(self, timeout_seconds: float) -> None:
        """Set the idle timeout used when this session returns to idle."""
        self._idle_timeout = timeout_seconds

    def start(self, proc: asyncio.subprocess.Process) -> None:
        """Attach to a spawned container process and start background monitors.

        Starts:
        - stderr reader (log capture)
        - proc monitor (detects unexpected container death via proc.wait())

        Output is handled by the IPC watcher, not by reading stdout.
        """
        self.proc = proc
        self._dead = False
        self._runtime_alive_after_proc_exit = False
        if proc.stderr is None:
            raise RuntimeError(
                _MISSING_STDERR_PIPE_ERROR.format(container_name=self.container_name)
            )
        self._stderr_task = create_background_task(
            self._read_stderr(proc.stderr),
            name=f"stderr-{self.container_name}",
        )
        self._proc_monitor_task = create_background_task(
            self._monitor_proc(proc),
            name=f"proc-monitor-{self.container_name}",
        )
        if sys.platform == "darwin":
            self._runtime_monitor_task = create_background_task(
                self._monitor_runtime_while_cli_alive(proc),
                name=f"runtime-monitor-{self.container_name}",
            )
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
        """Stop the container and clean up resources."""
        self._cancel_idle_timer()
        self._dead = True

        # Write close sentinel
        with contextlib.suppress(OSError):
            write_ipc_close_sentinel(self.group_folder)

        try:
            # Stop the container
            if self.proc and self.proc.returncode is None:
                await graceful_stop(self.proc, self.container_name)

            # Force remove (handles cases where graceful stop didn't clean up)
            await docker_rm_force(self.container_name)
        finally:
            # IPC requests can still arrive while the worker is draining.
            self._destroy_security_gate()

        # Cancel background tasks
        for task in (self._proc_monitor_task, self._runtime_monitor_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Signal anyone waiting on query_done
        self._query_done.set()
        self._query_progress.set()

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
        logger.info(
            "Session idle timeout, destroying",
            group=self.group_folder,
            container=self.container_name,
        )

        async def _idle_teardown() -> None:
            if self._on_idle_expire is not None:
                try:
                    await self._on_idle_expire()
                except Exception:  # noqa: BLE001 - idle callback is best-effort teardown and must not block destroy.
                    logger.exception(
                        "Idle callback failed",
                        group=self.group_folder,
                    )
            await destroy_session(self.group_folder)

        create_background_task(
            _idle_teardown(),
            name=f"idle-destroy-{self.group_folder}",
        )

    async def _monitor_proc(self, proc: asyncio.subprocess.Process) -> None:
        """Monitor the container process and detect unexpected death.

        Waits for proc.wait() to return. Docker's ``docker run`` process is a
        good proxy for the container lifetime, but Apple Container can let the
        ``container run`` client exit while the VM-backed container keeps
        running. In that case, use runtime-state polling and keep the
        session usable for IPC until the actual container stops.

        A clean exit (code 0) means the container shut down intentionally
        (for example, reset_context) -- NOT a crash.
        """
        exit_code = await proc.wait()
        if self._dead:
            return

        if await self._runtime_probe(self.container_name):
            self._runtime_alive_after_proc_exit = True
            logger.info(
                "Container CLI process exited while runtime container remains running",
                group=self.group_folder,
                container=self.container_name,
                exit_code=exit_code,
            )
            await self._monitor_runtime_container(exit_code)
            return

        await self._mark_container_exited(exit_code)

    async def _monitor_runtime_while_cli_alive(self, proc: asyncio.subprocess.Process) -> None:
        """Detect Apple runtime container death even if ``container run`` hangs."""
        seen_running = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._runtime_monitor_policy.start_grace_seconds

        while proc.returncode is None and not self._dead:
            running = await self._runtime_probe(self.container_name)
            if running:
                seen_running = True
            elif seen_running:
                logger.warning(
                    "Runtime container stopped while CLI process remained alive",
                    group=self.group_folder,
                    container=self.container_name,
                )
                await self._kill_stuck_runtime_cli(proc)
                return
            elif loop.time() >= deadline:
                logger.warning(
                    "Runtime container did not start while CLI process remained alive",
                    group=self.group_folder,
                    container=self.container_name,
                )
                await self._kill_stuck_runtime_cli(proc)
                return
            await _wait_for_runtime_poll_interval(
                self._runtime_monitor_policy.poll_interval_seconds
            )

    async def _kill_stuck_runtime_cli(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                proc.wait(), timeout=self._runtime_monitor_policy.cli_kill_wait_seconds
            )
        await reap_apple_runtime_orphans(self.container_name)
        await self._mark_container_exited(1)

    async def _monitor_runtime_container(self, cli_exit_code: int) -> None:
        """Poll runtime state after the CLI client exits before the container."""
        while await self._runtime_probe(self.container_name):
            await _wait_for_runtime_poll_interval(
                self._runtime_monitor_policy.poll_interval_seconds
            )
        await self._mark_container_exited(cli_exit_code)

    async def _mark_container_exited(self, exit_code: int) -> None:
        """Record actual container exit and unblock any in-flight query waiter."""
        was_stopping = self._dead
        self._runtime_alive_after_proc_exit = False
        self._dead = True
        self._destroy_security_gate()

        if was_stopping:
            self._query_done.set()
            self._query_progress.set()
        elif not self._query_done.is_set():
            if exit_code == 0:
                logger.info(
                    "Container exited cleanly without pulse (likely reset_context)",
                    group=self.group_folder,
                    container=self.container_name,
                    exit_code=exit_code,
                )
            else:
                self._died_before_pulse = True
                logger.warning(
                    "Container died before query-done pulse",
                    group=self.group_folder,
                    container=self.container_name,
                    exit_code=exit_code,
                )
            self._query_done.set()
            self._query_progress.set()

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


async def create_session(  # noqa: PLR0913 - session creation needs explicit process, path, and timeout inputs.
    group_folder: str,
    container_name: str,
    proc: asyncio.subprocess.Process,
    *,
    data_dir: Path,
    idle_timeout: float,
    invocation_ts: float = 0.0,
) -> ContainerSession:
    """Create and register a session for a group.

    Assumes the caller has already cleared any stale container with the
    same name *before* spawning ``proc``.  Stale IPC files are cleaned here.

    IMPORTANT: Do NOT call ``docker_rm_force(container_name)`` here.
    By this point the container is already running — force-removing it
    would race with (and potentially kill) the just-spawned process.
    The existing session's ``stop()`` call below handles its container,
    and the caller (``_cold_start``) handles stale-name cleanup pre-spawn.
    """
    # Destroy existing session if any
    old = _sessions.pop(group_folder, None)
    if old is not None:
        await old.stop()

    # Clean stale IPC files before the starting container reads them.
    # preserve_initial=True because the container is still starting and
    # reads initial.json on boot.
    clean_ipc_input_dir(group_folder, preserve_initial=True)
    _clean_ipc_output(data_dir, group_folder)

    session = ContainerSession(
        group_folder,
        container_name,
        invocation_ts=invocation_ts,
    )
    session.set_idle_timeout(idle_timeout)
    session.start(proc)
    _sessions[group_folder] = session

    logger.info(
        "Session created",
        group=group_folder,
        container=container_name,
    )
    return session


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
