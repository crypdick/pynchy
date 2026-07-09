"""OpenAI Agents SDK agent core implementation."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING, Any, cast

from agents import Agent, ApplyPatchTool, Runner, ShellTool, WebSearchTool
from agents.editor import ApplyPatchEditor, ApplyPatchOperation, ApplyPatchResult
from agents.mcp import (
    MCPServer,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
)

from agent_runner.core import AgentCoreConfig, AgentEvent
from agent_runner.cores._openai_tool_parsing import extract_tool_call, extract_tool_result

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from agent_runner.hooks import BeforeToolUseHook


def _log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    sys.stderr.write(f"[openai-core] {message}\n")
    sys.stderr.flush()


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
    except Exception as exc:  # allow: exception-handling; best-effort  # noqa: BLE001, RUF100
        _log(f"Tracing disable skipped: {exc}")


async def _run_before_tool_use_hooks(
    before_tool_hooks: list[BeforeToolUseHook] | None,
    command: str,
) -> str | None:
    """Run shell hooks and return an error string if one blocks execution."""
    if not before_tool_hooks:
        return None

    for hook_fn in before_tool_hooks:
        decision = await hook_fn("Bash", {"command": command})
        if not decision.allowed:
            _log(f"Command blocked by hook: {decision.reason}")
            return f"Command blocked by security policy: {decision.reason}"

    return None


# ---------------------------------------------------------------------------
# Shell executor — runs commands directly in the container
# ---------------------------------------------------------------------------


def _make_shell_executor(
    cwd: str,
    before_tool_hooks: list[BeforeToolUseHook] | None = None,
) -> Callable[[Any], Awaitable[str]]:
    """Create a shell executor bound to a specific working directory.

    Args:
        cwd: Working directory for shell commands.
        before_tool_hooks: Optional list of async hook functions with signature
            ``async (tool_name: str, tool_input: dict) -> HookDecision``.
            Each hook is called before the subprocess runs; if any returns
            ``allowed=False`` the command is blocked without execution.
    """

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

        if isinstance(commands, list | tuple):
            command = " && ".join(str(cmd) for cmd in commands)
        else:
            command = str(commands)

        timeout_ms = get_field(action, "timeout_ms") or get_field(data, "timeout_ms") or 120_000
        max_output_length = get_field(action, "max_output_length") or get_field(
            data, "max_output_length"
        )
        timeout_s = timeout_ms / 1000

        _log(f"Shell ({cwd}): {command[:200]}")

        # Run BEFORE_TOOL_USE hooks before subprocess execution.
        # Same hook signature as the Claude core: (tool_name, tool_input) -> HookDecision.
        blocked = await _run_before_tool_use_hooks(before_tool_hooks, command)
        if blocked is not None:
            return blocked

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            return f"Command timed out after {timeout_s}s"
        except Exception as exc:  # allow: exception-handling; return  # noqa: BLE001, RUF100
            return f"Shell error: {exc}"
        else:
            output = stdout.decode(errors="replace")
            if stderr:
                output += "\n" + stderr.decode(errors="replace")
            if isinstance(max_output_length, int) and max_output_length > 0:
                output = output[:max_output_length]
            return output

    return executor


# ---------------------------------------------------------------------------
# Patch editor — applies file patches directly in the container
# ---------------------------------------------------------------------------


def _create_patch_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _update_patch_file(path: Path, content: str) -> bool:
    if not path.exists():
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _delete_patch_file(path: Path) -> None:
    path.unlink(missing_ok=True)


class _ContainerPatchEditor(ApplyPatchEditor):
    """Applies patches to files on the container filesystem."""

    async def create_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            await asyncio.to_thread(
                _create_patch_file,
                Path(op.path),
                op.new_content or "",
            )
            return ApplyPatchResult(status="completed")
        except Exception as exc:  # allow: exception-handling; failed result  # noqa: BLE001, RUF100
            return ApplyPatchResult(status="failed", output=str(exc))

    async def update_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            updated = await asyncio.to_thread(
                _update_patch_file,
                Path(op.path),
                op.new_content or "",
            )
            if not updated:
                return ApplyPatchResult(status="failed", output=f"File not found: {op.path}")
            return ApplyPatchResult(status="completed")
        except Exception as exc:  # allow: exception-handling; failed result  # noqa: BLE001, RUF100
            return ApplyPatchResult(status="failed", output=str(exc))

    async def delete_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            await asyncio.to_thread(_delete_patch_file, Path(op.path))
            return ApplyPatchResult(status="completed")
        except Exception as exc:  # allow: exception-handling; failed result  # noqa: BLE001, RUF100
            return ApplyPatchResult(status="failed", output=str(exc))


# ---------------------------------------------------------------------------
# Stream event → AgentEvent translation
# ---------------------------------------------------------------------------


def _handle_raw_response_event(event: Any) -> AgentEvent | None:
    """Token-level text deltas, or reasoning/thinking content (o-series models)."""
    delta = getattr(event.data, "delta", None)
    if delta and isinstance(delta, str):
        return AgentEvent(type="text", data={"text": delta})
    if hasattr(event.data, "type") and "reasoning" in str(getattr(event.data, "type", "")):
        text = getattr(event.data, "text", None) or getattr(event.data, "summary", None)
        if text:
            return AgentEvent(type="thinking", data={"thinking": text})
    return None


def _handle_tool_call_item(item: Any) -> AgentEvent:
    tool_name, tool_input = extract_tool_call(item)
    if not tool_input:
        _log(f"Tool call parsed without input: tool={tool_name}")
    return AgentEvent(
        type="tool_use",
        data={"tool_name": tool_name, "tool_input": tool_input or {}},
    )


def _handle_tool_call_output_item(item: Any) -> AgentEvent:
    tool_result_id, output = extract_tool_result(item)
    return AgentEvent(
        type="tool_result",
        data={
            "tool_result_id": tool_result_id,
            "tool_result_content": output,
            "tool_result_is_error": False,
        },
    )


def _handle_message_output_item(item: Any) -> AgentEvent | None:
    from agents import ItemHelpers

    text = ItemHelpers.text_message_output(item)
    if text:
        return AgentEvent(type="text", data={"text": text})
    return None


def _handle_reasoning_item(item: Any) -> AgentEvent | None:
    text = getattr(item, "text", None) or ""
    summary_parts = getattr(item, "summary", None)
    if summary_parts and isinstance(summary_parts, list):
        text = "\n".join(getattr(s, "text", str(s)) for s in summary_parts)
    if text:
        return AgentEvent(type="thinking", data={"thinking": text})
    return None


_RUN_ITEM_HANDLERS: dict[str, Callable[[Any], AgentEvent | None]] = {
    "tool_call_item": _handle_tool_call_item,
    "tool_call_output_item": _handle_tool_call_output_item,
    "message_output_item": _handle_message_output_item,
    "reasoning_item": _handle_reasoning_item,
}


def _handle_run_item_stream_event(event: Any) -> AgentEvent | None:
    handler = _RUN_ITEM_HANDLERS.get(event.item.type)
    if handler is None:
        return None
    return handler(event.item)


def _stdio_server(name: str, spec: dict[str, Any]) -> MCPServerStdio:
    """Build a stdio MCP server from a command-based spec."""
    params: dict[str, Any] = {"command": spec["command"]}
    if "args" in spec:
        params["args"] = spec.get("args", [])
    if "env" in spec and spec["env"] is not None:
        params["env"] = spec["env"]
    return MCPServerStdio(params=cast("Any", params), name=name)


def _http_server_params(spec: dict[str, Any]) -> dict[str, Any]:
    """Build shared HTTP-style MCP params from a URL-based spec."""
    params: dict[str, Any] = {"url": spec["url"]}
    if spec.get("headers"):
        params["headers"] = spec["headers"]
    return params


def _sse_server(name: str, spec: dict[str, Any]) -> MCPServerSse:
    """Build an SSE MCP server."""
    return MCPServerSse(params=cast("Any", _http_server_params(spec)), name=name)


def _streamable_http_server(name: str, spec: dict[str, Any]) -> MCPServerStreamableHttp:
    """Build a streamable-HTTP MCP server."""
    return MCPServerStreamableHttp(
        params=cast("Any", _http_server_params(spec)),
        name=name,
    )


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
        self._before_tool_hooks: list[BeforeToolUseHook] = []
        self._mcp_servers: list[MCPServer] = []
        self._mcp_stack = contextlib.AsyncExitStack()
        previous = _normalize_response_id(config.session_id)
        self._previous_response_id: str | None = previous
        self._session_id: str | None = previous

    def _build_mcp_server(
        self, name: str, spec: dict[str, Any]
    ) -> MCPServerStdio | MCPServerSse | MCPServerStreamableHttp | None:
        """Build an MCP server from a generic config dict."""
        if "command" in spec:
            return _stdio_server(name, spec)

        transport = spec.get("type") or spec.get("transport")
        if transport is None and "url" in spec:
            transport = "sse"

        if transport == "sse":
            return _sse_server(name, spec)

        if transport in ("streamable_http", "http"):
            return _streamable_http_server(name, spec)

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
                ShellTool(
                    executor=_make_shell_executor(
                        self.config.cwd,
                        before_tool_hooks=self._before_tool_hooks,
                    )
                ),
                ApplyPatchTool(editor=_ContainerPatchEditor()),
                WebSearchTool(),
            ],
            mcp_servers=self._mcp_servers,
        )

    async def _initialize_runtime(self) -> None:
        # Convert config.mcp_servers dict → MCPServer* instances
        for name, spec in self.config.mcp_servers.items():
            built = self._build_mcp_server(name, spec)
            if built is not None:
                self._mcp_servers.append(built)

        # Enter MCP server async contexts
        for server in self._mcp_servers:
            await self._mcp_stack.enter_async_context(server)

        # Build system instructions
        instructions = (
            "You are a helpful assistant running inside a container. "
            "You have shell access and can edit files."
        )
        if self.config.system_prompt_append:
            instructions += "\n\n" + self.config.system_prompt_append

        model = self.config.extra.get("model", "openai/gpt-5.5")
        self._model_primary = model
        self._instructions = instructions

        # Build security hooks list via the shared single-source roster so this
        # core enforces exactly the same gate as the Claude/claude-cli cores.
        from agent_runner.hooks import before_tool_use_roster, load_hooks

        self._before_tool_hooks = before_tool_use_roster(load_hooks(self.config.plugin_hooks))

        _log(
            f"Creating agent with model={self._model_primary}, "
            f"mcp_servers={len(self._mcp_servers)}, "
            f"security_hooks={len(self._before_tool_hooks)}"
        )
        self._agent = self._make_agent(self._model_primary)

    async def start(self) -> None:
        """Initialize OpenAI Agent with tools and MCP servers."""
        _disable_tracing()
        try:
            await self._initialize_runtime()
        except Exception:  # allow: exception-handling; init cleanup  # noqa: BLE001, RUF100
            await self._mcp_stack.aclose()
            raise

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query and yield AgentEvents."""
        if self._agent is None:
            raise RuntimeError("OpenAIAgentCore not started (call start() first)")

        _log(f"Starting query (previous_response_id: {self._previous_response_id or 'none'})...")
        if not isinstance(self._model_primary, str):
            raise TypeError("OpenAIAgentCore not started (missing model)")
        async for event in self._run_streamed(prompt, self._model_primary):
            yield event

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
            agent_event: AgentEvent | None = None
            if event.type == "raw_response_event":
                agent_event = _handle_raw_response_event(event)
            elif event.type == "run_item_stream_event":
                agent_event = _handle_run_item_stream_event(event)
            elif event.type == "agent_updated_stream_event":
                _log(f"Agent updated: {event.new_agent.name}")

            if agent_event is not None:
                yield agent_event

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
        try:
            await self._mcp_stack.aclose()
        except Exception as exc:  # allow: exception-handling; cleanup  # noqa: BLE001, RUF100
            _log(f"Error closing MCP server: {exc}")
        self._mcp_servers.clear()
        self._agent = None

    @property
    def session_id(self) -> str | None:
        """Return current session ID (OpenAI response_id)."""
        return self._session_id
