"""OpenAI Agents SDK agent core implementation."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

from agents import Agent, ApplyPatchTool, Runner, ShellTool, WebSearchTool
from agents.editor import ApplyPatchEditor, ApplyPatchOperation, ApplyPatchResult
from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

from ..core import AgentCoreConfig, AgentEvent
from ._openai_tool_parsing import extract_tool_call, extract_tool_result


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


def _make_shell_executor(cwd: str, before_tool_hooks: list | None = None):
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
        if before_tool_hooks:
            for hook_fn in before_tool_hooks:
                decision = await hook_fn("Bash", {"command": command})
                if not decision.allowed:
                    _log(f"Command blocked by hook: {decision.reason}")
                    return f"Command blocked by security policy: {decision.reason}"

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
            return ApplyPatchResult(status="failed", output=str(exc))

    async def update_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            path = Path(op.path)
            if not path.exists():
                return ApplyPatchResult(status="failed", output=f"File not found: {op.path}")
            path.write_text(op.new_content or "")
            return ApplyPatchResult(status="completed")
        except Exception as exc:
            return ApplyPatchResult(status="failed", output=str(exc))

    async def delete_file(self, op: ApplyPatchOperation) -> ApplyPatchResult:
        from pathlib import Path

        try:
            Path(op.path).unlink(missing_ok=True)
            return ApplyPatchResult(status="completed")
        except Exception as exc:
            return ApplyPatchResult(status="failed", output=str(exc))


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
        self._before_tool_hooks: list = []
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
        self._model_fallback = self.config.extra.get("fallback_model", "openai/gpt-5.2-codex")
        self._instructions = instructions

        # Build security hooks list (same hooks used by the Claude core)
        from agent_runner.hooks import HookEvent, load_hooks
        from agent_runner.security.bash_gate import bash_security_hook
        from agent_runner.security.guard_git import guard_git_hook

        self._before_tool_hooks = [bash_security_hook, guard_git_hook]
        # Add plugin-provided BEFORE_TOOL_USE hooks
        agnostic = load_hooks(self.config.plugin_hooks)
        self._before_tool_hooks.extend(agnostic.get(HookEvent.BEFORE_TOOL_USE, []))

        _log(
            f"Creating agent with model={self._model_primary}, "
            f"fallback={self._model_fallback}, "
            f"mcp_servers={len(self._mcp_servers)}, "
            f"security_hooks={len(self._before_tool_hooks)}"
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
                # Token-level text deltas
                delta = getattr(event.data, "delta", None)
                if delta and isinstance(delta, str):
                    yield AgentEvent(type="text", data={"text": delta})
                # Reasoning/thinking content (o-series models)
                elif hasattr(event.data, "type") and "reasoning" in str(
                    getattr(event.data, "type", "")
                ):
                    text = getattr(event.data, "text", None) or getattr(event.data, "summary", None)
                    if text:
                        yield AgentEvent(type="thinking", data={"thinking": text})

            elif event.type == "run_item_stream_event":
                item = event.item

                if item.type == "tool_call_item":
                    tool_name, tool_input = extract_tool_call(item)
                    if not tool_input:
                        _log(f"Tool call parsed without input: tool={tool_name}")
                    yield AgentEvent(
                        type="tool_use",
                        data={
                            "tool_name": tool_name,
                            "tool_input": tool_input or {},
                        },
                    )

                elif item.type == "tool_call_output_item":
                    tool_result_id, output = extract_tool_result(item)
                    yield AgentEvent(
                        type="tool_result",
                        data={
                            "tool_result_id": tool_result_id,
                            "tool_result_content": output,
                            "tool_result_is_error": False,
                        },
                    )

                elif item.type == "message_output_item":
                    from agents import ItemHelpers

                    text = ItemHelpers.text_message_output(item)
                    if text:
                        yield AgentEvent(type="text", data={"text": text})

                elif item.type == "reasoning_item":
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
