"""Persistent container sessions — keep containers alive between message rounds.

A ContainerSession owns a running container process and its I/O readers.
Sessions live in a module-level registry keyed by group_folder.  The session
provides methods to send IPC messages and wait for query completion, decoupling
container lifecycle from individual message processing.

Two paths through run_agent():
  Cold path: first message or after reset — spawn container, start readers
  Warm path: subsequent messages — send IPC message, wait for query done
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time

from pynchy.config import get_settings
from pynchy.container_runner._process import (
    OnOutput,
    extract_marker_outputs,
    is_query_done_pulse,
)
from pynchy.logger import logger
from pynchy.runtime.runtime import get_runtime


class SessionDiedError(Exception):
    """Raised when the container process exits unexpectedly."""


class ContainerSession:
    """Owns a running container process and provides query-level interaction.

    The background stdout reader runs for the lifetime of the container,
    dispatching output events to a mutable callback and detecting the
    session-update pulse that signals query completion.
    """

    def __init__(self, group_folder: str, container_name: str) -> None:
        self.group_folder = group_folder
        self.container_name = container_name
        self.proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
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
        """Attach to a spawned container process and start background readers."""
        self.proc = proc
        self._dead = False
        assert proc.stdout is not None
        assert proc.stderr is not None
        self._stdout_task = asyncio.ensure_future(self._read_stdout(proc.stdout))
        self._stderr_task = asyncio.ensure_future(self._read_stderr(proc.stderr))
        self._reset_idle_timer()

    def set_output_handler(self, on_output: OnOutput | None) -> None:
        """Set callback for the next query and clear the query_done event."""
        self._on_output = on_output
        self._query_done.clear()
        self._died_before_pulse = False
        self._cancel_idle_timer()

    async def send_ipc_message(self, text: str) -> None:
        """Write a JSON message file to the container's IPC input directory."""
        s = get_settings()
        input_dir = s.data_dir / "ipc" / self.group_folder / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}.json"
        filepath = input_dir / filename
        temp_path = filepath.with_suffix(".json.tmp")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: (
                temp_path.write_text(json.dumps({"type": "message", "text": text})),
                temp_path.rename(filepath),
            ),
        )

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
        s = get_settings()
        input_dir = s.data_dir / "ipc" / self.group_folder / "input"
        with contextlib.suppress(OSError):
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")

        # Stop the container
        if self.proc and self.proc.returncode is None:
            from pynchy.container_runner._process import _graceful_stop

            await _graceful_stop(self.proc, self.container_name)

        # Force remove (handles cases where graceful stop didn't clean up)
        await _docker_rm_force(self.container_name)

        # Cancel background tasks
        for task in (self._stdout_task, self._stderr_task):
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
        from pynchy.utils import create_background_task

        create_background_task(
            destroy_session(self.group_folder),
            name=f"idle-destroy-{self.group_folder}",
        )

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        """Long-lived stdout reader — dispatches outputs and detects query-done pulses."""
        parse_buffer = ""
        spawn_time = time.monotonic()
        first_chunk_logged = False

        while True:
            chunk = await stream.read(8192)
            if not chunk:
                # EOF — container exited
                self._dead = True
                if not self._query_done.is_set():
                    # Container died before emitting the query-done pulse
                    self._died_before_pulse = True
                self._query_done.set()
                logger.info(
                    "Session stdout EOF",
                    group=self.group_folder,
                    container=self.container_name,
                )
                return

            text = chunk.decode(errors="replace")

            if not first_chunk_logged:
                elapsed_ms = (time.monotonic() - spawn_time) * 1000
                first_chunk_logged = True
                logger.info(
                    "Session first stdout",
                    group=self.group_folder,
                    elapsed_ms=round(elapsed_ms),
                )

            # Parse marker-delimited outputs
            parse_buffer += text
            outputs, parse_buffer = extract_marker_outputs(parse_buffer, self.group_folder)

            for output in outputs:
                # Dispatch to current output handler
                if self._on_output is not None:
                    try:
                        await self._on_output(output)
                    except Exception as exc:
                        logger.error(
                            "Session output callback failed",
                            group=self.group_folder,
                            error=str(exc),
                        )

                # Detect query-done pulse
                if is_query_done_pulse(output):
                    self._query_done.set()
                    self._on_output = None
                    self._reset_idle_timer()

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


async def create_session(
    group_folder: str,
    container_name: str,
    proc: asyncio.subprocess.Process,
    idle_timeout_override: float | None = None,
) -> ContainerSession:
    """Create and register a new session for a group.

    Cleans up any stale container with the same name and stale IPC input
    files before registering.
    """
    # Destroy existing session if any
    old = _sessions.pop(group_folder, None)
    if old is not None:
        await old.stop()

    # Force-remove stale container with same name
    await _docker_rm_force(container_name)

    # Clean stale IPC input files
    _clean_ipc_input(group_folder)

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


def _clean_ipc_input(group_folder: str) -> None:
    """Remove stale IPC input files for a group."""
    s = get_settings()
    input_dir = s.data_dir / "ipc" / group_folder / "input"
    if not input_dir.is_dir():
        return
    for f in input_dir.iterdir():
        with contextlib.suppress(OSError):
            f.unlink()


async def _docker_rm_force(container_name: str) -> None:
    """Force-remove a container by name, ignoring expected errors."""
    try:
        proc = await asyncio.create_subprocess_exec(
            get_runtime().cli,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except OSError as exc:
        # OSError covers FileNotFoundError (CLI missing) and other
        # process-spawn failures — expected in degraded environments.
        logger.debug("docker rm -f failed", container=container_name, err=str(exc))
