"""Request-response IPC for service tools (email, calendar, passwords).

Service tools write a request to the tasks/ directory and poll the
responses/ directory for the result. The host processes the request
(applying policy middleware) and writes the response back.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from mcp.types import TextContent

from agent_runner.agent_tools._ipc import IPC_DIR, write_ipc_file

RESPONSES_DIR = IPC_DIR / "responses"


async def ipc_service_request(
    tool_name: str,
    request: dict,
    timeout: float = 300,
) -> list[TextContent]:
    """Write an IPC service request and wait for the host's response.

    The host will apply policy middleware before processing. If the
    request is denied by policy, the response will contain an error.

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

    # Write request to tasks/ (picked up by host IPC watcher)
    write_ipc_file(IPC_DIR / "tasks", request)

    # Poll for response
    response_file = RESPONSES_DIR / f"{request_id}.json"
    elapsed = 0.0
    poll_interval = 0.5

    while elapsed < timeout:
        if response_file.exists():
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

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return [TextContent(type="text", text="Error: Request timed out waiting for host response")]
