"""Request-response IPC for service tools (calendar, X, Slack, etc.).

Service tools write a request to the requests/ directory and wait for the
host to write a response to the responses/ directory. Uses watchdog for
efficient file notification instead of polling.

The host processes the request (applying policy middleware) and writes
the response back via atomic tmp-file→rename.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from mcp.types import TextContent
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import _ipc


def _responses_dir() -> Path:
    """Return the active runtime's directory for host IPC responses."""
    return _ipc.get_agent_tool_runtime().ipc_dir / "responses"


class _ResponseWatcher(FileSystemEventHandler):
    """Watchdog handler that signals an asyncio.Event when the target response file appears.

    Runs in watchdog's background thread; uses ``call_soon_threadsafe`` to
    wake the async event loop. Matches the pattern in ``ipc.py:_InputEventHandler``.
    """

    def __init__(
        self,
        target_filename: str,
        loop: asyncio.AbstractEventLoop,
        event: asyncio.Event,
    ) -> None:
        super().__init__()
        self._target = target_filename
        self._loop = loop
        self._event = event

    def _signal_if_match(self, path: str | bytes) -> None:
        if Path(os.fsdecode(path)).name == self._target:
            self._loop.call_soon_threadsafe(self._event.set)

    def on_created(self, event: object) -> None:  # noqa: V105
        if isinstance(event, FileCreatedEvent):
            self._signal_if_match(event.src_path)

    def on_moved(self, event: object) -> None:  # noqa: V105
        # Host writes atomically (tmp -> rename), which produces a moved event
        if isinstance(event, FileMovedEvent):
            self._signal_if_match(event.dest_path)


def _read_response(response_file: Path) -> list[TextContent]:
    """Read and delete a response file, returning MCP TextContent."""
    try:
        response = json.loads(response_file.read_text(encoding="utf-8"))
    finally:
        response_file.unlink(missing_ok=True)

    if response.get("error"):
        return [TextContent(type="text", text=f"Error: {response['error']}")]

    return [
        TextContent(
            type="text",
            text=json.dumps(response.get("result", {}), indent=2),
        )
    ]


def _ensure_responses_dir(responses_dir: Path) -> None:
    responses_dir.mkdir(parents=True, exist_ok=True)


def _response_file_exists(response_file: Path) -> bool:
    return response_file.exists()


async def _wait_for_response_file(
    response_file: Path,
    wakeup: asyncio.Event,
    response_timeout_seconds: float,
) -> None:
    """Wait for a response file, using watchdog wakeups plus periodic polling."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + response_timeout_seconds

    while True:
        if await asyncio.to_thread(_response_file_exists, response_file):
            return

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError

        try:
            await asyncio.wait_for(wakeup.wait(), timeout=min(0.2, remaining))
            wakeup.clear()
        except TimeoutError:
            pass


async def ipc_service_request(
    tool_name: str,
    request: dict[str, object],
    response_timeout_seconds: float | None = None,
    *,
    type_override: str | None = None,
    guarded_action_id: str | None = None,
) -> list[TextContent]:
    """Write an IPC service request and wait for the host's response.

    Uses watchdog to efficiently wait for the response file instead of
    polling. The host writes responses atomically (tmp→rename), so we
    handle both ``on_created`` and ``on_moved`` events.

    Args:
        tool_name: Name of the service tool (e.g. "read_email")
        request: Request payload (tool-specific fields)
        response_timeout_seconds: Seconds to wait for response (default 5 min for
            human approval)
        type_override: Optional IPC type string. When set, overrides the default
            ``service:{tool_name}`` prefix. Use this for tools that require a
            different dispatch prefix on the host (e.g. ``"ask_user:ask"``).

    Returns:
        MCP TextContent with the result or error message.
    """
    request_id = guarded_action_id or uuid.uuid4().hex
    request_kind = type_override or f"service:{tool_name}"
    timeout_seconds = (
        response_timeout_seconds
        if response_timeout_seconds is not None
        else _ipc.get_agent_tool_runtime().service_request_timeout_seconds
    )

    responses_dir = _responses_dir()
    response_file = responses_dir / f"{request_id}.json"
    await asyncio.to_thread(_ensure_responses_dir, responses_dir)

    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()

    handler = _ResponseWatcher(response_file.name, loop, wakeup)
    observer = Observer()
    observer.schedule(handler, str(responses_dir), recursive=False)
    observer.daemon = True
    observer_started = False
    try:
        observer.start()
        observer_started = True
    except OSError:
        # If the host has exhausted inotify watches, polling still preserves
        # correctness. Watchdog remains an optimization, not a hard dependency.
        pass

    try:
        # Double-check: response might already exist (race with host)
        if await asyncio.to_thread(_response_file_exists, response_file):
            return _read_response(response_file)

        # Write request to requests/ (picked up by host IPC watcher).
        # Done *after* observer is started so we can't miss the response.
        _ipc.write_request_file(request_kind, request, request_id=request_id, reply_to="responses")

        # Second check: host may have responded between observer.start()
        # and now (especially fast in tests or local setups)
        if await asyncio.to_thread(_response_file_exists, response_file):
            return _read_response(response_file)

        # Watchdog should wake this promptly; polling covers missed/unavailable events.
        await _wait_for_response_file(response_file, wakeup, timeout_seconds)

        return _read_response(response_file)

    except TimeoutError:
        return [TextContent(type="text", text="Error: Request timed out waiting for host response")]
    finally:
        if observer_started:
            observer.stop()
            observer.join(timeout=2)
