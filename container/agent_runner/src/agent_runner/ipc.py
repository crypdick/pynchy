"""IPC protocol — file-based input/output for host↔container communication.

Input protocol:
  Initial: ContainerInput JSON read from /workspace/ipc/input/initial.json
           (written by host before container start, deleted after read)
  IPC:     Follow-up messages written as JSON files to /workspace/ipc/input/
           Sentinel: /workspace/ipc/input/_close — signals session end

Output protocol:
  Each event is written as a JSON file to /workspace/ipc/output/.
  Filenames are monotonic nanosecond timestamps ({ns}.json) for guaranteed
  ordering. Files are written atomically (write .json.tmp, then rename).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .models import ContainerInput, ContainerOutput

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
INITIAL_INPUT_FILE = IPC_INPUT_DIR / "initial.json"

IPC_OUTPUT_DIR = Path("/workspace/ipc/output")


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
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


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


def drain_ipc_input() -> list[str]:
    """Drain all pending IPC input messages. Returns messages found."""
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")

        messages: list[str] = []
        for file_path in files:
            try:
                data = json.loads(file_path.read_text())
                file_path.unlink()
                if isinstance(data, dict) and data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except (json.JSONDecodeError, OSError) as exc:
                log(f"Failed to process input file {file_path.name}: {exc}")
                with contextlib.suppress(OSError):
                    file_path.unlink()
        return messages
    except OSError as exc:
        log(f"IPC drain error: {exc}")
        return []


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

    def _signal_if_relevant(self, path_str: str) -> None:
        p = Path(path_str)
        # Wake up for .json message files or the _close sentinel
        if p.suffix == ".json" or p.name == "_close":
            self._loop.call_soon_threadsafe(self._event.set)

    def on_created(self, event: Any) -> None:
        if isinstance(event, FileCreatedEvent):
            self._signal_if_relevant(event.src_path)

    def on_moved(self, event: Any) -> None:
        # Host writes atomically (tmp -> rename), which produces a moved event
        if isinstance(event, FileMovedEvent):
            self._signal_if_relevant(event.dest_path)


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel.

    Uses watchdog to detect new files in IPC_INPUT_DIR instead of polling.
    Returns the messages as a single string, or None if _close.
    """
    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()

    handler = _InputEventHandler(loop, wakeup)
    observer = Observer()
    observer.schedule(handler, str(IPC_INPUT_DIR), recursive=False)
    observer.daemon = True
    observer.start()

    try:
        while True:
            if should_close():
                return None
            messages = drain_ipc_input()
            if messages:
                return "\n".join(messages)
            # Wait until watchdog signals new file activity, then re-check
            await wakeup.wait()
            wakeup.clear()
    finally:
        observer.stop()
        observer.join(timeout=2)
