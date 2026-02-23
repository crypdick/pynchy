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


def _normalize_response_id(value: str | None) -> str | None:
    """Return a valid OpenAI response ID (resp*), or None if invalid."""
    if not value:
        return None
    return value if value.startswith("resp") else None


def _disable_tracing() -> None:
    """Disable OpenAI Agents SDK tracing to avoid 401s in LiteLLM mode."""
    try:
        from agents import set_tracing_disabled

        set_tracing_disabled(disabled=True)
        _log("Tracing disabled")
    except Exception as exc:
        _log(f"Tracing disable skipped: {exc}")


def _is_model_not_found(exc: Exception) -> bool:
    """Return True if the error indicates the model is unavailable."""
    message = str(exc).lower()
    return (
        "model_not_found" in message
        or "does not exist" in message
        or "no healthy deployments for this model" in message
    )


# ---------------------------------------------------------------------------
# Shell executor — runs commands directly in the container
# ---------------------------------------------------------------------------


def _make_shell_executor(cwd: str):
    """Create a shell executor bound to a specific working directory."""

    async def executor(request: Any) -> str:
        """Execute a shell command inside the container."""
        def get_field(obj: Any, name: str) -> Any:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(name)
            return getattr(obj, name, None)

        data = get_field(request, "data")
        action = get_field(data, "action") or get_field(request, "action")

        commands = get_field(action, "commands")
        if commands is None:
            command = get_field(action, "command")
            commands = [command] if command else None

        if not commands:
            return "Shell tool request missing commands."

        if isinstance(commands, (list, tuple)):
            command = " && ".join(str(cmd) for cmd in commands)
        else:
            command = str(commands)

        timeout_ms = get_field(action, "timeout_ms") or get_field(data, "timeout_ms") or 120_000
        max_output_length = get_field(action, "max_output_length") or get_field(
            data, "max_output_length"
        )
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
            if isinstance(max_output_length, int) and max_output_length > 0:
                output = output[:max_output_length]
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
        self._instructions: str | None = None
        self._model_primary: str | None = None
        self._model_fallback: str | None = None
        self._mcp_servers: list[MCPServerStdio | MCPServerSse | MCPServerStreamableHttp] = []
        self._mcp_contexts: list[Any] = []
        previous = _normalize_response_id(config.session_id)
        self._previous_response_id: str | None = previous
        self._session_id: str | None = previous

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

    def _make_agent(self, model: str) -> Agent:
        if self._instructions is None:
            raise RuntimeError("OpenAIAgentCore not started (missing instructions)")
        return Agent(
            name="pynchy",
            instructions=self._instructions,
            model=model,
            tools=[
                ShellTool(executor=_make_shell_executor(self.config.cwd)),
                ApplyPatchTool(editor=_ContainerPatchEditor()),
                WebSearchTool(),
            ],
            mcp_servers=self._mcp_servers,
        )

    async def start(self) -> None:
        """Initialize OpenAI Agent with tools and MCP servers."""
        _disable_tracing()
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

        model = self.config.extra.get("model", "openai/gpt-5.3-codex")
        self._model_primary = model
        self._model_fallback = self.config.extra.get(
            "fallback_model", "openai/gpt-5.2-codex"
        )
        self._instructions = instructions
        _log(
            f"Creating agent with model={self._model_primary}, "
            f"fallback={self._model_fallback}, "
            f"mcp_servers={len(self._mcp_servers)}"
        )
        self._agent = self._make_agent(self._model_primary)

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query and yield AgentEvents."""
        if self._agent is None:
            raise RuntimeError("OpenAIAgentCore not started (call start() first)")

        _log(f"Starting query (previous_response_id: {self._previous_response_id or 'none'})...")

        for attempt, model in enumerate((self._model_primary, self._model_fallback), start=1):
            if not model:
                continue
            emitted_any = False
            try:
                if attempt > 1:
                    _log(f"Retrying with fallback model={model}")
                async for event in self._run_streamed(prompt, model):
                    emitted_any = True
                    yield event
                return
            except Exception as exc:
                if (
                    attempt == 1
                    and self._model_fallback
                    and _is_model_not_found(exc)
                    and not emitted_any
                ):
                    _log(f"Primary model failed ({exc}); trying fallback")
                    continue
                raise

    async def _run_streamed(self, prompt: str, model: str) -> AsyncIterator[AgentEvent]:
        """Run a single streamed request for the given model."""
        agent = (
            self._agent
            if self._agent is not None and model == self._model_primary
            else self._make_agent(model)
        )

        result = Runner.run_streamed(
            agent,
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
                    text = getattr(event.data, "text", None) or getattr(
                        event.data, "summary", None
                    )
                    if text:
                        yield AgentEvent(type="thinking", data={"thinking": text})

            elif event.type == "run_item_stream_event":
                item = event.item
                if item.type == "tool_call_item":
                    # Extract tool name and arguments from the raw item
                    raw = getattr(item, "raw_item", item)
                    tool_name = (
                        getattr(item, "tool_name", None)
                        or getattr(item, "name", None)
                        or getattr(raw, "tool_name", None)
                        or getattr(raw, "name", None)
                    )
                    tool_input = (
                        getattr(item, "arguments", None)
                        or getattr(item, "input", None)
                        or getattr(raw, "arguments", None)
                    )

                    func = getattr(raw, "function", None)
                    if func is not None:
                        tool_name = tool_name or getattr(func, "name", None)
                        tool_input = tool_input or getattr(func, "arguments", None)

                    call = getattr(raw, "call", None)
                    if call is not None:
                        tool_name = tool_name or getattr(call, "name", None)
                        tool_input = tool_input or getattr(call, "arguments", None)

                    if tool_name in (None, "", "unknown_tool", "function"):
                        action = getattr(raw, "action", None)
                        if action is not None:
                            if hasattr(action, "command") or hasattr(action, "commands"):
                                tool_name = "shell"
                                if tool_input is None:
                                    cmd = getattr(action, "command", None)
                                    cmds = getattr(action, "commands", None)
                                    if cmd is not None:
                                        tool_input = {"command": cmd}
                                    elif cmds is not None:
                                        tool_input = {"commands": cmds}
                            elif hasattr(action, "patch") or hasattr(action, "path"):
                                tool_name = "apply_patch"
                        else:
                            data = getattr(raw, "data", None)
                            action = getattr(data, "action", None) if data is not None else None
                            if action is not None and (
                                hasattr(action, "command") or hasattr(action, "commands")
                            ):
                                tool_name = "shell"
                                if tool_input is None:
                                    cmd = getattr(action, "command", None)
                                    cmds = getattr(action, "commands", None)
                                    if cmd is not None:
                                        tool_input = {"command": cmd}
                                    elif cmds is not None:
                                        tool_input = {"commands": cmds}

                    if tool_name in (None, "", "unknown_tool"):
                        def _extract_mapping(mapping: dict[str, Any]) -> None:
                            nonlocal tool_name, tool_input
                            if tool_name in (None, "", "unknown_tool"):
                                for key in ("tool_name", "name", "tool", "type"):
                                    value = mapping.get(key)
                                    if value:
                                        tool_name = value
                                        break
                            if tool_input is None:
                                tool_input = mapping.get("arguments") or mapping.get("input")
                            action = mapping.get("action")
                            if tool_name in (None, "", "unknown_tool") and isinstance(action, dict):
                                cmds = action.get("commands")
                                cmd = action.get("command")
                                if cmds or cmd:
                                    tool_name = "shell"
                                    if tool_input is None:
                                        tool_input = {"commands": cmds} if cmds else {"command": cmd}

                        data_dump = None
                        if hasattr(raw, "model_dump"):
                            try:
                                data_dump = raw.model_dump()
                            except Exception:
                                data_dump = None
                        if data_dump is None and hasattr(raw, "__dict__"):
                            data_dump = vars(raw)
                        if isinstance(data_dump, dict):
                            _extract_mapping(data_dump)
                            inner = data_dump.get("data")
                            if isinstance(inner, dict):
                                _extract_mapping(inner)

                        if tool_name in (None, "", "unknown_tool"):
                            raw_type = type(raw).__name__.lower()
                            if "shell" in raw_type:
                                tool_name = "shell"
                            elif "patch" in raw_type:
                                tool_name = "apply_patch"
                            elif "search" in raw_type:
                                tool_name = "web_search"
                            else:
                                tool_name = getattr(raw, "type", None) or "unknown_tool"

                    if tool_input is None:
                        tool_input = getattr(raw, "input", None)
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
