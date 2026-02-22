"""OpenAI Agents SDK agent core implementation."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from typing import Any

from agents import Agent, ApplyPatchTool, Runner, ShellTool, WebSearchTool
from agents.editor import ApplyPatchEditor, ApplyPatchOperation, ApplyPatchResult
from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

from ..core import AgentCoreConfig, AgentEvent


def _log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[openai-core] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Shell executor — runs commands directly in the container
# ---------------------------------------------------------------------------


def _make_shell_executor(cwd: str):
    """Create a shell executor bound to a specific working directory."""

    async def executor(request: Any) -> str:
        """Execute a shell command inside the container."""
        command = request.command if hasattr(request, "command") else str(request)
        timeout_ms = getattr(request, "timeout_ms", 120_000)
        timeout_s = timeout_ms / 1000

        _log(f"Shell ({cwd}): {command[:200]}")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            output = stdout.decode(errors="replace")
            if stderr:
                output += "\n" + stderr.decode(errors="replace")
            return output
        except TimeoutError:
            proc.kill()
            return f"Command timed out after {timeout_s}s"
        except Exception as exc:
            return f"Shell error: {exc}"

    return executor


# ---------------------------------------------------------------------------
# Patch editor — applies file patches directly in the container
# ---------------------------------------------------------------------------


class _ContainerPatchEditor(ApplyPatchEditor):
    """Applies patches to files on the container filesystem."""

    async def create_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            path = Path(op.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(op.new_content or "")
            return ApplyPatchResult(status="completed")
        except Exception as exc:
            return ApplyPatchResult(status="failed", error=str(exc))

    async def update_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            path = Path(op.path)
            if not path.exists():
                return ApplyPatchResult(status="failed", error=f"File not found: {op.path}")
            path.write_text(op.new_content or "")
            return ApplyPatchResult(status="completed")
        except Exception as exc:
            return ApplyPatchResult(status="failed", error=str(exc))

    async def delete_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            Path(op.path).unlink(missing_ok=True)
            return ApplyPatchResult(status="completed")
        except Exception as exc:
            return ApplyPatchResult(status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# OpenAIAgentCore
# ---------------------------------------------------------------------------


class OpenAIAgentCore:
    """Agent core implementation using OpenAI Agents SDK."""

    def __init__(self, config: AgentCoreConfig) -> None:
        self.config = config
        self._agent: Agent | None = None
        self._mcp_servers: list[MCPServerStdio | MCPServerSse | MCPServerStreamableHttp] = []
        self._mcp_contexts: list[Any] = []
        self._previous_response_id: str | None = config.session_id
        self._session_id: str | None = config.session_id

    def _build_mcp_server(
        self, name: str, spec: dict[str, Any]
    ) -> MCPServerStdio | MCPServerSse | MCPServerStreamableHttp | None:
        """Build an MCP server from a generic config dict."""
        if "command" in spec:
            params: dict[str, Any] = {"command": spec["command"]}
            if "args" in spec:
                params["args"] = spec.get("args", [])
            if "env" in spec and spec["env"] is not None:
                params["env"] = spec["env"]
            return MCPServerStdio(params=params, name=name)

        transport = spec.get("type") or spec.get("transport")
        if transport is None and "url" in spec:
            transport = "sse"

        if transport in ("sse",):
            params = {"url": spec["url"]}
            if "headers" in spec and spec["headers"]:
                params["headers"] = spec["headers"]
            return MCPServerSse(params=params, name=name)

        if transport in ("streamable_http", "http"):
            params = {"url": spec["url"]}
            if "headers" in spec and spec["headers"]:
                params["headers"] = spec["headers"]
            return MCPServerStreamableHttp(params=params, name=name)

        _log(f"Skipping MCP server '{name}': unsupported spec {spec}")
        return None

    async def start(self) -> None:
        """Initialize OpenAI Agent with tools and MCP servers."""
        # Convert config.mcp_servers dict → MCPServer* instances
        for name, spec in self.config.mcp_servers.items():
            server = self._build_mcp_server(name, spec)
            if server is not None:
                self._mcp_servers.append(server)

        # Enter MCP server async contexts
        for server in self._mcp_servers:
            ctx = await server.__aenter__()
            self._mcp_contexts.append(ctx)

        # Build system instructions
        instructions = (
            "You are a helpful assistant running inside a container. "
            "You have shell access and can edit files."
        )
        if self.config.system_prompt_append:
            instructions += "\n\n" + self.config.system_prompt_append

        model = self.config.extra.get("model", "gpt-5.2")
        _log(f"Creating agent with model={model}, mcp_servers={len(self._mcp_servers)}")

        self._agent = Agent(
            name="pynchy",
            instructions=instructions,
            model=model,
            tools=[
                ShellTool(executor=_make_shell_executor(self.config.cwd)),
                ApplyPatchTool(editor=_ContainerPatchEditor()),
                WebSearchTool(),
            ],
            mcp_servers=self._mcp_servers,
        )

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query and yield AgentEvents."""
        if self._agent is None:
            raise RuntimeError("OpenAIAgentCore not started (call start() first)")

        _log(f"Starting query (previous_response_id: {self._previous_response_id or 'none'})...")

        result = Runner.run_streamed(
            self._agent,
            input=prompt,
            previous_response_id=self._previous_response_id,
            auto_previous_response_id=True,
        )

        async for event in result.stream_events():
            if event.type == "raw_response_event":
                # Token-level text deltas — yield as text events
                delta = getattr(event.data, "delta", None)
                if delta and isinstance(delta, str):
                    yield AgentEvent(type="text", data={"text": delta})
                # Check for reasoning/thinking content (o-series models)
                elif hasattr(event.data, "type") and "reasoning" in str(
                    getattr(event.data, "type", "")
                ):
                    text = getattr(event.data, "text", None) or getattr(event.data, "summary", None)
                    if text:
                        yield AgentEvent(type="thinking", data={"thinking": text})

            elif event.type == "run_item_stream_event":
                item = event.item
                if item.type == "tool_call_item":
                    # Extract tool name and arguments from the raw item
                    raw = getattr(item, "raw_item", item)
                    tool_name = getattr(raw, "name", None) or getattr(raw, "type", "unknown_tool")
                    tool_input = getattr(raw, "arguments", None)
                    if isinstance(tool_input, str):
                        try:
                            tool_input = json.loads(tool_input)
                        except (json.JSONDecodeError, TypeError):
                            tool_input = {"raw": tool_input}
                    yield AgentEvent(
                        type="tool_use",
                        data={
                            "tool_name": tool_name,
                            "tool_input": tool_input or {},
                        },
                    )

                elif item.type == "tool_call_output_item":
                    output = getattr(item, "output", "")
                    yield AgentEvent(
                        type="tool_result",
                        data={
                            "tool_result_id": getattr(item, "call_id", ""),
                            "tool_result_content": str(output) if output else "",
                            "tool_result_is_error": False,
                        },
                    )

                elif item.type == "message_output_item":
                    # Full message output — extract text content
                    from agents import ItemHelpers

                    text = ItemHelpers.text_message_output(item)
                    if text:
                        yield AgentEvent(type="text", data={"text": text})

                elif item.type == "reasoning_item":
                    # Reasoning/thinking from o-series models
                    text = getattr(item, "text", None) or ""
                    summary_parts = getattr(item, "summary", None)
                    if summary_parts and isinstance(summary_parts, list):
                        text = "\n".join(getattr(s, "text", str(s)) for s in summary_parts)
                    if text:
                        yield AgentEvent(type="thinking", data={"thinking": text})

            elif event.type == "agent_updated_stream_event":
                _log(f"Agent updated: {event.new_agent.name}")

        # After stream completes, capture response ID for session continuity
        self._previous_response_id = result.last_response_id
        self._session_id = result.last_response_id

        # Yield final result event
        yield AgentEvent(
            type="result",
            data={
                "result": result.final_output,
                "result_metadata": {
                    "subtype": "result",
                    "session_id": result.last_response_id,
                    "is_error": False,
                },
            },
        )

        _log(f"Query done. response_id={result.last_response_id}")

    async def stop(self) -> None:
        """Clean up MCP server contexts."""
        for server in reversed(self._mcp_servers):
            try:
                await server.__aexit__(None, None, None)
            except Exception as exc:
                _log(f"Error closing MCP server: {exc}")
        self._mcp_servers.clear()
        self._mcp_contexts.clear()
        self._agent = None

    @property
    def session_id(self) -> str | None:
        """Return current session ID (OpenAI response_id)."""
        return self._session_id
