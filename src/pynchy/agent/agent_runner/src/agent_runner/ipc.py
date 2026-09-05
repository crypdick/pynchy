"""IPC protocol — file-based input/output for host↔container communication.

Input protocol:
  Initial: ContainerInput JSON read from /run/pynchy/input/initial.json
           (written by host before container start, deleted after read)
  IPC:     Follow-up messages written as JSON files to /run/pynchy/input/
           Sentinel: /run/pynchy/input/_close — signals session end

Output protocol:
  Each event is written as a JSON file to /run/pynchy/output/.
  Filenames are monotonic nanosecond timestamps ({ns}.json) for guaranteed
  ordering. Files are written atomically (write .json.tmp, then rename).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .models import ContainerInput, ContainerOutput

IPC_ROOT = Path(os.environ.get("PYNCHY_IPC_DIR", "/run/pynchy"))
IPC_INPUT_DIR = IPC_ROOT / "input"
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
INITIAL_INPUT_FILE = IPC_INPUT_DIR / "initial.json"

IPC_OUTPUT_DIR = IPC_ROOT / "output"


@dataclass(frozen=True)
class IpcMessage:
    """Follow-up message delivered to a warm persistent container."""

    text: str
    turn_id: str | None = None
    query_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_output(output: ContainerOutput) -> None:
    """Write an output event as a JSON file to the IPC output directory.

    Uses monotonic_ns timestamps for filenames to guarantee ordering.
    Writes atomically: data goes to a .json.tmp file first, then is
    renamed to .json so the host-side watcher never sees partial writes.
    """
    IPC_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{time.monotonic_ns()}.json"
    final_path = IPC_OUTPUT_DIR / filename
    tmp_path = final_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(output.to_dict()))
    tmp_path.rename(final_path)


def log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    sys.stderr.write(f"[agent-runner] {message}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Input functions
# ---------------------------------------------------------------------------


def read_initial_input() -> ContainerInput:
    """Read initial ContainerInput from the IPC input file.

    The host writes ``initial.json`` to the IPC input directory before
    starting the container.  We read it once on startup, parse it into a
    ``ContainerInput``, and delete the file so ``drain_ipc_input()`` never
    picks it up as a follow-up message.

    Raises ``FileNotFoundError`` if the file is missing (container was
    started without the host writing initial input).
    """
    data = json.loads(INITIAL_INPUT_FILE.read_text())
    container_input = ContainerInput.from_dict(data)
    INITIAL_INPUT_FILE.unlink()
    return container_input


def should_close() -> bool:
    """Check for _close sentinel."""
    if IPC_INPUT_CLOSE_SENTINEL.exists():
        with contextlib.suppress(OSError):
            IPC_INPUT_CLOSE_SENTINEL.unlink()
        return True
    return False


def _parse_ipc_message(data: object) -> IpcMessage | None:
    if not isinstance(data, dict) or data.get("type") != "message" or not data.get("text"):
        return None

    text = data["text"]
    if not isinstance(text, str):
        return None

    turn_id = data.get("turn_id")
    query_id = data.get("query_id")
    raw_metadata = data.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    return IpcMessage(
        text=text,
        turn_id=turn_id if isinstance(turn_id, str) else None,
        query_id=query_id if isinstance(query_id, str) else None,
        metadata=metadata,
    )


def drain_ipc_messages() -> list[IpcMessage]:
    """Drain all pending IPC input messages. Returns parsed follow-up envelopes."""
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")
    except OSError as exc:
        log(f"IPC drain error: {exc}")
        return []
    else:
        messages: list[IpcMessage] = []
        for file_path in files:
            try:
                data = json.loads(file_path.read_text())
                file_path.unlink()
                if message := _parse_ipc_message(data):
                    messages.append(message)
            except (json.JSONDecodeError, OSError) as exc:
                log(f"Failed to process input file {file_path.name}: {exc}")
                with contextlib.suppress(OSError):
                    file_path.unlink()
        return messages


def drain_ipc_input() -> list[str]:
    """Drain all pending IPC input messages. Returns only message text."""
    return [message.text for message in drain_ipc_messages()]


class _InputEventHandler(FileSystemEventHandler):
    """Watchdog handler that signals an asyncio.Event when input files appear.

    Runs in the watchdog background thread; uses call_soon_threadsafe to wake
    the async event loop.  Matches the pattern used by the host-side watcher
    (src/pynchy/ipc/_watcher.py).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        super().__init__()
        self._loop = loop
        self._event = event

    def _signal_if_relevant(self, path_str: str | bytes) -> None:
        # watchdog types event paths as str | bytes; normalize to str.
        p = Path(os.fsdecode(path_str))
        # Wake up for .json message files or the _close sentinel
        if p.suffix == ".json" or p.name == "_close":
            self._loop.call_soon_threadsafe(self._event.set)

    def on_created(self, event: object) -> None:  # noqa: V105
        if isinstance(event, FileCreatedEvent):
            self._signal_if_relevant(event.src_path)

    def on_moved(self, event: object) -> None:  # noqa: V105
        # Host writes atomically (tmp -> rename), which produces a moved event
        if isinstance(event, FileMovedEvent):
            self._signal_if_relevant(event.dest_path)


def _combine_ipc_messages(messages: list[IpcMessage]) -> IpcMessage:
    text = "\n".join(message.text for message in messages)
    turn_id = next((message.turn_id for message in reversed(messages) if message.turn_id), None)
    query_id = next((message.query_id for message in reversed(messages) if message.query_id), None)
    metadata: dict[str, Any] = {}
    for message in messages:
        metadata.update(message.metadata)
    return IpcMessage(text=text, turn_id=turn_id, query_id=query_id, metadata=metadata)


async def wait_for_ipc_followup() -> IpcMessage | None:
    """Wait for an incoming IPC follow-up envelope or _close sentinel.

    Uses watchdog to detect incoming files in IPC_INPUT_DIR instead of polling.
    Returns combined message text and metadata, or None if _close.
    """
    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()

    handler = _InputEventHandler(loop, wakeup)
    observer = Observer()
    observer.schedule(handler, str(IPC_INPUT_DIR), recursive=False)
    observer.daemon = True
    observer_started = False
    try:
        observer.start()
        observer_started = True
    except OSError:
        # If watchdog cannot allocate a filesystem watch, the loop below still
        # polls input periodically. File IPC must remain correct without events.
        pass

    try:
        while True:
            if should_close():
                return None
            messages = drain_ipc_messages()
            if messages:
                return _combine_ipc_messages(messages)
            # Watchdog should wake this promptly; polling covers missed/unavailable events.
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=0.2)
                wakeup.clear()
            except TimeoutError:
                pass
    finally:
        if observer_started:
            observer.stop()
            observer.join(timeout=2)
