"""Request-response IPC for service tools (calendar, X, Slack, etc.).

Service tools write a request to the tasks/ directory and wait for the
host to write a response to the responses/ directory. Uses watchdog for
efficient file notification instead of polling.

The host processes the request (applying policy middleware) and writes
the response back via atomic tmp-file→rename.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from mcp.types import TextContent
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agent_runner.agent_tools._ipc import IPC_DIR, write_ipc_file

RESPONSES_DIR = IPC_DIR / "responses"


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

    def _signal_if_match(self, path_str: str) -> None:
        if Path(path_str).name == self._target:
            self._loop.call_soon_threadsafe(self._event.set)

    def on_created(self, event: Any) -> None:
        if isinstance(event, FileCreatedEvent):
            self._signal_if_match(event.src_path)

    def on_moved(self, event: Any) -> None:
        # Host writes atomically (tmp -> rename), which produces a moved event
        if isinstance(event, FileMovedEvent):
            self._signal_if_match(event.dest_path)


def _read_response(response_file: Path) -> list[TextContent]:
    """Read and delete a response file, returning MCP TextContent."""
    try:
        response = json.loads(response_file.read_text())
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


async def ipc_service_request(
    tool_name: str,
    request: dict,
    timeout: float = 300,
) -> list[TextContent]:
    """Write an IPC service request and wait for the host's response.

    Uses watchdog to efficiently wait for the response file instead of
    polling. The host writes responses atomically (tmp→rename), so we
    handle both ``on_created`` and ``on_moved`` events.

    Args:
        tool_name: Name of the service tool (e.g. "read_email")
        request: Request payload (tool-specific fields)
        timeout: Seconds to wait for response (default 5 min for human approval)

    Returns:
        MCP TextContent with the result or error message.
    """
    request_id = uuid.uuid4().hex
    request["type"] = f"service:{tool_name}"
    request["request_id"] = request_id

    response_file = RESPONSES_DIR / f"{request_id}.json"
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()

    handler = _ResponseWatcher(response_file.name, loop, wakeup)
    observer = Observer()
    observer.schedule(handler, str(RESPONSES_DIR), recursive=False)
    observer.daemon = True
    observer.start()

    try:
        # Double-check: response might already exist (race with host)
        if response_file.exists():
            return _read_response(response_file)

        # Write request to tasks/ (picked up by host IPC watcher).
        # Done *after* observer is started so we can't miss the response.
        write_ipc_file(IPC_DIR / "tasks", request)

        # Second check: host may have responded between observer.start()
        # and now (especially fast in tests or local setups)
        if response_file.exists():
            return _read_response(response_file)

        # Wait for watchdog to signal the response file appeared
        await asyncio.wait_for(wakeup.wait(), timeout=timeout)

        return _read_response(response_file)

    except TimeoutError:
        return [TextContent(type="text", text="Error: Request timed out waiting for host response")]
    finally:
        observer.stop()
        observer.join(timeout=2)
