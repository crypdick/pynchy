"""OpenAI Agents SDK agent core implementation."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agents import (
    Agent,
    ApplyPatchTool,
    ItemHelpers,
    ModelSettings,
    RunConfig,
    Runner,
    ShellTool,
    WebSearchTool,
    apply_diff,
    set_tracing_disabled,
)
from agents.editor import ApplyPatchEditor, ApplyPatchOperation, ApplyPatchResult
from agents.mcp import (
    MCPServer,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
)

from agent_runner import hooks
from agent_runner.events import (
    ResultEvent,
    ResultMetadata,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)

from ._openai_tool_parsing import extract_tool_call, extract_tool_result
from .openai_shell import make_shell_executor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from agent_runner.core import AgentCoreConfig
    from agent_runner.events import AgentEvent
    from agent_runner.hooks import BeforeToolUseHook


_NOT_STARTED_MISSING_INSTRUCTIONS = "OpenAIAgentCore not started (missing instructions)"
_NOT_STARTED_CALL_START_FIRST = "OpenAIAgentCore not started (call start() first)"
_NOT_STARTED_MISSING_MODEL = "OpenAIAgentCore not started (missing model)"


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
        set_tracing_disabled(disabled=True)
        _log("Tracing disabled")
    except Exception as exc:  # allow: exception-handling; best-effort  # noqa: BLE001
        _log(f"Tracing disable skipped: {exc}")


def _metadata_as_strings(metadata: object) -> dict[str, str] | None:
    if not isinstance(metadata, dict):
        return None
    return {str(key): str(value) for key, value in metadata.items()}


def build_mcp_server(
    name: str,
    spec: dict[str, Any],
) -> MCPServerStdio | MCPServerSse | MCPServerStreamableHttp | None:
    """Build one SDK MCP server from its container configuration."""
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


# ---------------------------------------------------------------------------
# Patch editor — applies file patches directly in the container
# ---------------------------------------------------------------------------


def _create_patch_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _update_patch_file(path: Path, diff: str) -> bool:
    if not path.exists():
        return False
    # Responses API apply_patch calls carry V4A diffs, not replacement content.
    content = apply_diff(path.read_text(encoding="utf-8"), diff)
    path.write_text(content, encoding="utf-8")
    return True


def _delete_patch_file(path: Path) -> None:
    path.unlink(missing_ok=True)


class ContainerPatchEditor(ApplyPatchEditor):
    """Applies patches to files on the container filesystem."""

    def __init__(
        self,
        before_tool_hooks: list[BeforeToolUseHook] | None = None,
        *,
        cwd: str = ".",
    ) -> None:
        self._before_tool_hooks = before_tool_hooks or []
        self._cwd = Path(cwd)

    async def _security_failure(self, op: ApplyPatchOperation) -> ApplyPatchResult | None:
        tool_input = {"path": op.path, "diff": op.diff or ""}
        for hook_fn in self._before_tool_hooks:
            decision = await hook_fn("apply_patch", tool_input)
            if not decision.allowed:
                return ApplyPatchResult(
                    status="failed",
                    output=f"Patch blocked by security policy: {decision.reason}",
                )
        return None

    async def create_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:  # noqa: V105
        if failure := await self._security_failure(op):
            return failure
        try:
            content = apply_diff("", op.diff or "", mode="create")
            await asyncio.to_thread(
                _create_patch_file,
                self._cwd / op.path,
                content,
            )
            return ApplyPatchResult(status="completed")
        except Exception as exc:  # allow: exception-handling; failed result  # noqa: BLE001
            return ApplyPatchResult(status="failed", output=str(exc))

    async def update_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:  # noqa: V105
        if failure := await self._security_failure(op):
            return failure
        try:
            updated = await asyncio.to_thread(
                _update_patch_file,
                self._cwd / op.path,
                op.diff or "",
            )
            if not updated:
                return ApplyPatchResult(status="failed", output=f"File not found: {op.path}")
            return ApplyPatchResult(status="completed")
        except Exception as exc:  # allow: exception-handling; failed result  # noqa: BLE001
            return ApplyPatchResult(status="failed", output=str(exc))

    async def delete_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:  # noqa: V105
        if failure := await self._security_failure(op):
            return failure
        try:
            await asyncio.to_thread(_delete_patch_file, self._cwd / op.path)
            return ApplyPatchResult(status="completed")
        except Exception as exc:  # allow: exception-handling; failed result  # noqa: BLE001
            return ApplyPatchResult(status="failed", output=str(exc))


# ---------------------------------------------------------------------------
# Stream event → AgentEvent translation
# ---------------------------------------------------------------------------


def _handle_raw_response_event(event: object) -> AgentEvent | None:
    """Token-level text deltas, or reasoning/thinking content (o-series models)."""
    event_data = cast("Any", event).data
    delta = getattr(event_data, "delta", None)
    if delta and isinstance(delta, str):
        return TextEvent(text=delta)
    if hasattr(event_data, "type") and "reasoning" in str(getattr(event_data, "type", "")):
        text = getattr(event_data, "text", None) or getattr(event_data, "summary", None)
        if text:
            return ThinkingEvent(thinking=text)
    return None


def _handle_tool_call_item(item: object) -> AgentEvent:
    tool_name, tool_input = extract_tool_call(item)
    if not tool_input:
        _log(f"Tool call parsed without input: tool={tool_name}")
    return ToolUseEvent(
        tool_name=tool_name,
        tool_input=tool_input if isinstance(tool_input, dict) else {},
    )


def _handle_tool_call_output_item(item: object) -> AgentEvent:
    tool_result_id, output, is_error = extract_tool_result(item)
    return ToolResultEvent(
        tool_result_id=tool_result_id,
        tool_result_content=output,
        tool_result_is_error=is_error,
    )


def _handle_message_output_item(item: object) -> AgentEvent | None:
    text = ItemHelpers.text_message_output(cast("Any", item))
    if text:
        return TextEvent(text=text)
    return None


def _handle_reasoning_item(item: object) -> AgentEvent | None:
    text = getattr(item, "text", None) or ""
    summary_parts = getattr(item, "summary", None)
    if summary_parts and isinstance(summary_parts, list):
        text = "\n".join(getattr(s, "text", str(s)) for s in summary_parts)
    if text:
        return ThinkingEvent(thinking=text)
    return None


_RUN_ITEM_HANDLERS: dict[str, Callable[[object], AgentEvent | None]] = {
    "tool_call_item": _handle_tool_call_item,
    "tool_call_output_item": _handle_tool_call_output_item,
    "message_output_item": _handle_message_output_item,
    "reasoning_item": _handle_reasoning_item,
}


def _handle_run_item_stream_event(event: object) -> AgentEvent | None:
    item = cast("Any", event).item
    handler = _RUN_ITEM_HANDLERS.get(item.type)
    if handler is None:
        return None
    return handler(item)


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


class OpenAIAgentCore:  # noqa: V102
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

    def _make_agent(self, model: str) -> Agent:
        if self._instructions is None:
            raise RuntimeError(_NOT_STARTED_MISSING_INSTRUCTIONS)
        return Agent(
            name="pynchy",
            instructions=self._instructions,
            model=model,
            tools=[
                ShellTool(
                    executor=make_shell_executor(
                        self.config.cwd,
                        before_tool_hooks=self._before_tool_hooks,
                    )
                ),
                ApplyPatchTool(
                    editor=ContainerPatchEditor(
                        self._before_tool_hooks,
                        cwd=self.config.cwd,
                    )
                ),
                WebSearchTool(),
            ],
            mcp_servers=self._mcp_servers,
        )

    async def _initialize_runtime(self) -> None:
        # Convert config.mcp_servers dict → MCPServer* instances
        for name, spec in self.config.mcp_servers.items():
            built = build_mcp_server(name, spec)
            if built is not None:
                self._mcp_servers.append(built)

        # Enter MCP server async contexts
        for server in self._mcp_servers:
            # The SDK implements __aenter__/__aexit__ without return annotations,
            # so its published type does not satisfy the protocol statically.
            context_manager = cast("AbstractAsyncContextManager[MCPServer]", server)
            await self._mcp_stack.enter_async_context(context_manager)

        if not self.config.system_prompt_append:
            raise ValueError("OpenAI core requires a resolved Pynchy prompt context")
        instructions = self.config.system_prompt_append

        model = self.config.extra.get("model", "openai/gpt-5.5")
        self._model_primary = model
        self._instructions = instructions

        # Build security hooks list via the shared single-source roster so this
        # core enforces exactly the same gate as the Claude/claude-cli cores.
        hooks_enabled = bool(self.config.extra.get("pynchy_hooks_enabled", True))
        self._before_tool_hooks = (
            hooks.before_tool_use_roster(hooks.load_hooks(self.config.plugin_hooks))
            if hooks_enabled
            else []
        )

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
        except Exception:  # allow: exception-handling; init cleanup
            await self._mcp_stack.aclose()
            raise

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query and yield AgentEvents."""
        if self._agent is None:
            raise RuntimeError(_NOT_STARTED_CALL_START_FIRST)

        _log(f"Starting query (previous_response_id: {self._previous_response_id or 'none'})...")
        if not isinstance(self._model_primary, str):
            raise TypeError(_NOT_STARTED_MISSING_MODEL)
        async for event in self._run_streamed(prompt, self._model_primary):
            yield event

    async def _run_streamed(self, prompt: str, model: str) -> AsyncIterator[AgentEvent]:
        """Run a single streamed request for the given model."""
        agent = (
            self._agent
            if self._agent is not None and model == self._model_primary
            else self._make_agent(model)
        )

        run_config = None
        if metadata := _metadata_as_strings(self.config.extra.get("metadata")):
            run_config = RunConfig(
                model_settings=ModelSettings(metadata=metadata),
                trace_metadata=metadata,
            )

        result = Runner.run_streamed(
            agent,
            input=prompt,
            run_config=run_config,
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
        yield ResultEvent(
            result=result.final_output,
            result_metadata=ResultMetadata(
                subtype="result", session_id=result.last_response_id, is_error=False
            ),
        )

        _log(f"Query done. response_id={result.last_response_id}")

    async def stop(self) -> None:
        """Clean up MCP server contexts."""
        try:
            await self._mcp_stack.aclose()
        except Exception as exc:  # allow: exception-handling; cleanup  # noqa: BLE001
            _log(f"Error closing MCP server: {exc}")
        self._mcp_servers.clear()
        self._agent = None

    @property
    def session_id(self) -> str | None:
        """Return current session ID (OpenAI response_id)."""
        return self._session_id
