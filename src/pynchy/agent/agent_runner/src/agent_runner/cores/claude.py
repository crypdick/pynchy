"""Claude SDK agent core implementation."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import McpServerConfig, SdkPluginConfig, SystemPromptPreset

from agent_runner.core import AgentCoreConfig, AgentEvent
from agent_runner.cores._tools import BUILTIN_ALLOWED_TOOLS, DISALLOWED_TOOLS
from agent_runner.hooks import (
    AGNOSTIC_TO_CLAUDE,
    BeforeToolUseHook,
    HookEvent,
    before_tool_use_roster,
    load_hooks,
)
from agent_runner.transcript_archive import archive_transcript


def _log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[claude-core] {message}", file=sys.stderr, flush=True)  # allow: print-statements


# ---------------------------------------------------------------------------
# PreCompact hook (transcript archival)
# ---------------------------------------------------------------------------


def _create_pre_compact_hook() -> Callable[
    [dict[str, Any], str | None, HookContext],
    Awaitable[dict[str, Any]],
]:
    """Create a PreCompact hook that archives the transcript.

    The claude-cli core wires the same archival via a ``PreCompact`` command
    hook into ``agent_runner.transcript_archive``; both share that module.
    """

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        await archive_transcript(
            input_data.get("transcript_path", ""),
            input_data.get("session_id", ""),
        )
        return {}

    return hook


# ---------------------------------------------------------------------------
# PreToolUse security hook adapter
# ---------------------------------------------------------------------------


def _wrap_before_tool_use(
    hook_fn: BeforeToolUseHook,
) -> Callable[[dict[str, Any], str | None, HookContext], Awaitable[dict[str, Any]]]:
    """Wrap a BEFORE_TOOL_USE hook as a Claude SDK PreToolUse hook.

    Our agnostic hooks have signature (tool_name, tool_input) -> HookDecision.
    Claude SDK PreToolUse hooks expect (input_data, tool_use_id, context) -> dict.
    """

    async def wrapper(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        decision = await hook_fn(tool_name, tool_input)
        if not decision.allowed:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.reason or "Blocked by security policy",
                }
            }
        return {}

    return wrapper


# ---------------------------------------------------------------------------
# start() setup helpers
# ---------------------------------------------------------------------------


def _build_system_prompt(config: AgentCoreConfig) -> SystemPromptPreset | None:
    if not config.system_prompt_append:
        return None
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": config.system_prompt_append,
    }


def _build_claude_hooks(config: AgentCoreConfig) -> dict[str, list[HookMatcher]]:
    """Convert plugin-agnostic hooks to Claude SDK hook matchers.

    PreCompact gets a built-in transcript-archival hook appended. PreToolUse
    gets built-in security hooks first, then plugin hooks (first deny wins).
    """
    agnostic_hooks = load_hooks(config.plugin_hooks)
    claude_hooks: dict[str, list[HookMatcher]] = {}

    for event, funcs in agnostic_hooks.items():
        # BEFORE_TOOL_USE is handled separately below (needs _wrap_before_tool_use
        # adapter to translate between agnostic and Claude SDK signatures).
        if event == HookEvent.BEFORE_TOOL_USE:
            continue
        if event in AGNOSTIC_TO_CLAUDE:
            claude_hook_name = AGNOSTIC_TO_CLAUDE[event]
            if funcs:
                claude_hooks[claude_hook_name] = [HookMatcher(hooks=[func]) for func in funcs]

    if "PreCompact" not in claude_hooks:
        claude_hooks["PreCompact"] = []
    claude_hooks["PreCompact"].append(HookMatcher(hooks=[_create_pre_compact_hook()]))

    all_pre_tool_hooks = [
        _wrap_before_tool_use(fn) for fn in before_tool_use_roster(agnostic_hooks)
    ]
    if all_pre_tool_hooks:
        if "PreToolUse" not in claude_hooks:
            claude_hooks["PreToolUse"] = []
        # Single HookMatcher that matches all tools — hooks run in order,
        # first deny wins.
        claude_hooks["PreToolUse"].append(HookMatcher(hooks=all_pre_tool_hooks))

    return claude_hooks


def _build_allowed_tools(config: AgentCoreConfig) -> list[str]:
    allowed_tools = list(BUILTIN_ALLOWED_TOOLS)
    if "tools" in config.mcp_servers:
        allowed_tools.append("mcp__tools__*")
    for server_name in config.mcp_servers:
        pattern = f"mcp__{server_name}__*"
        if pattern not in allowed_tools:
            allowed_tools.append(pattern)
    return allowed_tools


def _discover_container_plugins() -> list[SdkPluginConfig]:
    """Discover Claude Code plugins baked into the container image."""
    plugins_dir = Path("/opt/plugins")
    if not plugins_dir.is_dir():
        return []
    return [
        SdkPluginConfig(type="local", path=str(p))
        for p in sorted(plugins_dir.iterdir())
        if p.is_dir()
    ]


# ---------------------------------------------------------------------------
# ClaudeAgentCore
# ---------------------------------------------------------------------------


class ClaudeAgentCore:
    """Agent core implementation using Claude SDK."""

    def __init__(self, config: AgentCoreConfig) -> None:
        self.config = config
        self._client: ClaudeSDKClient | None = None
        self._client_stack = contextlib.AsyncExitStack()
        self._session_id: str | None = config.session_id

    async def start(self) -> None:
        """Initialize Claude SDK client."""
        system_prompt = _build_system_prompt(self.config)
        claude_hooks = _build_claude_hooks(self.config)
        allowed_tools = _build_allowed_tools(self.config)

        _log(f"MCP servers config: {list(self.config.mcp_servers.keys())}")
        mcp_details = {
            k: {kk: vv for kk, vv in v.items() if kk != "env"}
            for k, v in self.config.mcp_servers.items()
        }
        _log(f"MCP servers details: {json.dumps(mcp_details)}")
        _log(f"Allowed tools: {allowed_tools}")

        plugins = _discover_container_plugins()
        if plugins:
            _log(f"Loading plugins: {[p['path'] for p in plugins]}")

        options = ClaudeAgentOptions(
            model="opus",
            cwd=self.config.cwd,
            resume=self.config.session_id,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            # Plan mode tools require interactive approval that headless
            # containers can't provide, causing an infinite resume loop.
            disallowed_tools=DISALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            settings='{"attribution": {"commit": "", "pr": ""}}',
            setting_sources=["project", "user"],
            # config.mcp_servers is a generic dict[str, dict[str, Any]]; the SDK
            # narrows it to its own McpServerConfig union at runtime.
            mcp_servers=cast("dict[str, McpServerConfig]", self.config.mcp_servers),
            # Agnostic hook names (from AGNOSTIC_TO_CLAUDE) are a superset of the
            # SDK's HookEvent literals, so the dict is typed str-keyed at assembly
            # time and handed to the SDK boundary as-is.
            hooks=cast(Any, claude_hooks) if claude_hooks else None,
            plugins=plugins,
        )

        # Create and enter client context
        self._client = ClaudeSDKClient(options)
        await self._client_stack.enter_async_context(self._client)

    def _system_event(self, message: SystemMessage) -> AgentEvent:
        """Map a system message and update the session when initialized."""
        if message.subtype == "init" and hasattr(message, "data"):
            sid = message.data.get("session_id")
            if sid:
                self._session_id = sid
                _log(f"Session initialized: {sid}")
        return AgentEvent(
            type="system",
            data={
                "system_subtype": message.subtype,
                "system_data": message.data if hasattr(message, "data") else {},
            },
        )

    @staticmethod
    def _tool_result_content(block: ToolResultBlock) -> str:
        """Flatten a Claude tool-result block into stored text content."""
        if isinstance(block.content, str):
            return block.content
        if isinstance(block.content, list):
            return json.dumps(block.content)
        return ""

    def _assistant_events(self, message: AssistantMessage) -> list[AgentEvent]:
        """Map assistant content blocks to AgentEvents."""
        events: list[AgentEvent] = []
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                events.append(AgentEvent(type="thinking", data={"thinking": block.thinking}))
            elif isinstance(block, ToolUseBlock):
                events.append(
                    AgentEvent(
                        type="tool_use",
                        data={"tool_name": block.name, "tool_input": block.input},
                    )
                )
            elif isinstance(block, ToolResultBlock):
                events.append(
                    AgentEvent(
                        type="tool_result",
                        data={
                            "tool_result_id": block.tool_use_id,
                            "tool_result_content": self._tool_result_content(block),
                            "tool_result_is_error": block.is_error,
                        },
                    )
                )
            elif isinstance(block, TextBlock):
                events.append(AgentEvent(type="text", data={"text": block.text}))
        return events

    def _result_event(self, message: ResultMessage) -> AgentEvent:
        """Map the terminal Claude SDK result message and update the session."""
        self._session_id = message.session_id or self._session_id
        result_meta = {
            "subtype": message.subtype,
            "duration_ms": message.duration_ms,
            "duration_api_ms": message.duration_api_ms,
            "is_error": message.is_error,
            "num_turns": message.num_turns,
            "session_id": message.session_id,
            "total_cost_usd": message.total_cost_usd,
            "usage": message.usage,
        }
        return AgentEvent(
            type="result",
            data={
                "result": getattr(message, "result", None),
                "result_metadata": result_meta,
            },
        )

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query using Claude SDK."""
        if self._client is None:
            raise RuntimeError("ClaudeAgentCore not started (call start() first)")

        _log(f"Starting query (session: {self._session_id or 'new'})...")

        await self._client.query(prompt)

        message_count = 0
        result_count = 0

        async for message in self._client.receive_response():
            message_count += 1

            if isinstance(message, SystemMessage):
                yield self._system_event(message)
            elif isinstance(message, AssistantMessage):
                for event in self._assistant_events(message):
                    yield event
            elif isinstance(message, ResultMessage):
                result_count += 1
                text_result = getattr(message, "result", None)
                _log(
                    f"Result #{result_count}: "
                    f"subtype={message.subtype}"
                    f"{f' text={text_result[:200]}' if text_result else ''}"
                )

                yield self._result_event(message)

        _log(f"Query done. Messages: {message_count}, results: {result_count}")

    async def stop(self) -> None:
        """Clean up Claude SDK client."""
        if self._client is not None:
            try:
                await self._client_stack.aclose()
            except Exception as exc:  # allow: exception-handling — cleanup; logged via _log()
                _log(f"Error during client cleanup: {exc}")
            finally:
                self._client = None

    @property
    def session_id(self) -> str | None:
        """Return current session ID."""
        return self._session_id
