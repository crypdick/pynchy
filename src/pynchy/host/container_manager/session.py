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
  is detected.  The session no longer reads stdout for output — only stderr is
  read (for log capture) and proc.wait() is monitored for unexpected death.
"""

from __future__ import annotations

import asyncio
import contextlib

from pynchy.config import get_settings
from pynchy.host.container_manager.process import (
    OnOutput,
    _docker_rm_force,
)
from pynchy.host.container_manager.ipc.write import clean_ipc_input_dir, write_ipc_close_sentinel, write_ipc_message
from pynchy.logger import logger
from pynchy.utils import create_background_task


class SessionDiedError(Exception):
    """Raised when the container process exits unexpectedly."""


class ContainerSession:
    """Owns a running container process and provides query-level interaction.

    Output is routed through file-based IPC: the container writes output files,
    the host IPC watcher processes them and calls back into the session via
    signal_query_done() and get_session_output_handler().

    The session monitors stderr (for log capture) and proc.wait() (for
    detecting unexpected container death).
    """

    def __init__(self, group_folder: str, container_name: str) -> None:
        self.group_folder = group_folder
        self.container_name = container_name
        self.proc: asyncio.subprocess.Process | None = None
        self._proc_monitor_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._on_output: OnOutput | None = None
        self._query_done = asyncio.Event()
        self._dead = False
        self._died_before_pulse = False
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_timeout: float = get_settings().idle_timeout

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and not self._dead

    def start(self, proc: asyncio.subprocess.Process) -> None:
        """Attach to a spawned container process and start background monitors.

        Starts:
        - stderr reader (log capture)
        - proc monitor (detects unexpected container death via proc.wait())

        Output is handled by the IPC watcher, not by reading stdout.
        """
        self.proc = proc
        self._dead = False
        if proc.stderr is None:
            raise RuntimeError(f"Container {self.container_name} spawned without stderr pipe")
        self._stderr_task = create_background_task(
            self._read_stderr(proc.stderr),
            name=f"stderr-{self.container_name}",
        )
        self._proc_monitor_task = create_background_task(
            self._monitor_proc(proc),
            name=f"proc-monitor-{self.container_name}",
        )
        self._reset_idle_timer()

    def set_output_handler(self, on_output: OnOutput | None) -> None:
        """Set callback for the next query and clear the query_done event."""
        self._on_output = on_output
        self._query_done.clear()
        self._died_before_pulse = False
        self._cancel_idle_timer()

    def signal_query_done(self) -> None:
        """Signal that the current query is complete.

        Called by the IPC watcher when it detects a query-done pulse in an
        output file.  Sets the _query_done event, clears the output handler,
        and resets the idle timer.
        """
        self._query_done.set()
        self._on_output = None
        self._reset_idle_timer()

    async def send_ipc_message(self, text: str) -> None:
        """Write a JSON message file to the container's IPC input directory."""
        write_ipc_message(self.group_folder, text)

    async def wait_for_query_done(self, timeout: float) -> None:
        """Wait for the query-done pulse or container death.

        Raises TimeoutError if the query doesn't complete in time.
        Raises SessionDiedError if the container exits *before* the pulse.

        If the pulse is detected and then the container exits (EOF after pulse),
        this is not an error — the pulse already confirmed query completion.
        """
        try:
            await asyncio.wait_for(self._query_done.wait(), timeout=timeout)
        except TimeoutError:
            logger.error(
                "Session query timed out",
                group=self.group_folder,
                container=self.container_name,
                timeout=timeout,
            )
            raise

        if self._died_before_pulse:
            raise SessionDiedError(f"Container {self.container_name} died during query")

    async def stop(self) -> None:
        """Stop the container and clean up resources."""
        self._cancel_idle_timer()
        self._dead = True

        # Write close sentinel
        with contextlib.suppress(OSError):
            write_ipc_close_sentinel(self.group_folder)

        # Stop the container
        if self.proc and self.proc.returncode is None:
            from pynchy.host.container_manager.process import _graceful_stop

            await _graceful_stop(self.proc, self.container_name)

        # Force remove (handles cases where graceful stop didn't clean up)
        await _docker_rm_force(self.container_name)

        # Cancel background tasks
        for task in (self._proc_monitor_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Signal anyone waiting on query_done
        self._query_done.set()

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

    def _on_idle_expired(self) -> None:
        """Called when the session has been idle for too long."""
        logger.info(
            "Session idle timeout, destroying",
            group=self.group_folder,
            container=self.container_name,
        )
        create_background_task(
            destroy_session(self.group_folder),
            name=f"idle-destroy-{self.group_folder}",
        )

    async def _monitor_proc(self, proc: asyncio.subprocess.Process) -> None:
        """Monitor the container process and detect unexpected death.

        Waits for proc.wait() to return.  If the process exits while a query
        is in-flight (i.e. _query_done is not yet set), sets _dead and
        _died_before_pulse, then signals _query_done so the caller unblocks.

        A clean exit (code 0) means the container shut down intentionally
        (reset_context, finished_work) -- NOT a crash.
        """
        exit_code = await proc.wait()

        self._dead = True

        if not self._query_done.is_set():
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

        logger.info(
            "Session proc exited",
            group=self.group_folder,
            container=self.container_name,
            exit_code=exit_code,
        )

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


def get_session(group_folder: str) -> ContainerSession | None:
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


def get_session_output_handler(group_folder: str) -> OnOutput | None:
    """Return the output handler for the active session of a group, or None.

    Used by the IPC watcher to dispatch output events to the correct callback.
    Returns None if no session is active or no handler is set.
    """
    session = get_session(group_folder)
    if session is None:
        return None
    return session._on_output


async def create_session(
    group_folder: str,
    container_name: str,
    proc: asyncio.subprocess.Process,
    idle_timeout_override: float | None = None,
) -> ContainerSession:
    """Create and register a new session for a group.

    Assumes the caller has already removed any stale container with the
    same name *before* spawning ``proc``.  Stale IPC files are cleaned here.

    IMPORTANT: Do NOT call ``_docker_rm_force(container_name)`` here.
    By this point the container is already running — force-removing it
    would race with (and potentially kill) the just-spawned process.
    The old session's ``stop()`` call below handles the previous container,
    and the caller (``_cold_start``) handles stale-name cleanup pre-spawn.
    """
    # Destroy existing session if any
    old = _sessions.pop(group_folder, None)
    if old is not None:
        await old.stop()

    # Clean stale IPC files from the previous session.
    # preserve_initial=True because the container is still starting and
    # reads initial.json on boot.
    clean_ipc_input_dir(group_folder, preserve_initial=True)
    _clean_ipc_output(group_folder)

    session = ContainerSession(group_folder, container_name)
    if idle_timeout_override is not None:
        session._idle_timeout = idle_timeout_override
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


def _clean_ipc_output(group_folder: str) -> None:
    """Remove stale IPC output files for a group.

    Called when creating a new session to prevent replay of output events
    from a previous (dead) session.  Output files are ephemeral mid-query
    artefacts — they have no value once the session that produced them is
    gone.
    """
    s = get_settings()
    output_dir = s.data_dir / "ipc" / group_folder / "output"
    if not output_dir.is_dir():
        return
    for f in output_dir.iterdir():
        with contextlib.suppress(OSError):
            f.unlink()
