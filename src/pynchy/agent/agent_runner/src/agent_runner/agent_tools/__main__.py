"""Run the MCP server or Codex's policy-preserving local bridge."""

import asyncio
import json
import sys
from typing import Any

from mcp.types import CallToolResult

from ._registry import HandlerResult, all_tools, tool_error
from ._server import call_tool, run_server


def _result_payload(result: HandlerResult) -> dict[str, Any]:
    if isinstance(result, CallToolResult):
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(result, tuple):
        content, structured = result
        return {
            "content": [item.model_dump(mode="json", by_alias=True) for item in content],
            "structuredContent": structured,
        }
    return {"content": [item.model_dump(mode="json", by_alias=True) for item in result]}


def _call_hex(tool_name: str, encoded_arguments: str) -> int:
    try:
        arguments = json.loads(bytes.fromhex(encoded_arguments))
        if not isinstance(arguments, dict):
            raise TypeError
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        sys.stderr.write("arguments must be hex-encoded JSON object\n")
        return 2

    result = (
        asyncio.run(call_tool(tool_name, arguments))
        if tool_name in {tool.name for tool in all_tools()}
        else tool_error(f"Unknown tool: {tool_name}")
    )
    sys.stdout.write(json.dumps(_result_payload(result)) + "\n")
    return int(isinstance(result, CallToolResult) and bool(result.isError))


def main() -> int:
    if len(sys.argv) == 1:
        asyncio.run(run_server())
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "call-hex":
        return _call_hex(sys.argv[2], sys.argv[3])
    sys.stderr.write("usage: python -m agent_runner.agent_tools [call-hex TOOL HEX_JSON]\n")
    return 2


raise SystemExit(main())
