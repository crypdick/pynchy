"""Claude SDK agent core implementation."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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

from ..core import AgentCoreConfig, AgentEvent
from ..hooks import AGNOSTIC_TO_CLAUDE, HookEvent, before_tool_use_roster, load_hooks
from ..transcript_archive import archive_transcript
from ._tools import BUILTIN_ALLOWED_TOOLS, DISALLOWED_TOOLS


def _log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[claude-core] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# PreCompact hook (transcript archival)
# ---------------------------------------------------------------------------


def _create_pre_compact_hook():
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


def _wrap_before_tool_use(hook_fn):
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
# ClaudeAgentCore
# ---------------------------------------------------------------------------


class ClaudeAgentCore:
    """Agent core implementation using Claude SDK."""

    def __init__(self, config: AgentCoreConfig) -> None:
        self.config = config
        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = config.session_id

    async def start(self) -> None:
        """Initialize Claude SDK client."""
        # Build system prompt
        system_prompt: dict[str, Any] | None = None
        if self.config.system_prompt_append:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": self.config.system_prompt_append,
            }

        # Load plugin hooks and convert to Claude SDK format
        agnostic_hooks = load_hooks(self.config.plugin_hooks)
        claude_hooks: dict[str, list] = {}

        for event, funcs in agnostic_hooks.items():
            # BEFORE_TOOL_USE is handled separately below (needs _wrap_before_tool_use
            # adapter to translate between agnostic and Claude SDK signatures).
            if event == HookEvent.BEFORE_TOOL_USE:
                continue
            if event in AGNOSTIC_TO_CLAUDE:
                claude_hook_name = AGNOSTIC_TO_CLAUDE[event]
                if funcs:
                    claude_hooks[claude_hook_name] = [HookMatcher(hooks=[func]) for func in funcs]

        # Add built-in PreCompact hook for transcript archival
        if "PreCompact" not in claude_hooks:
            claude_hooks["PreCompact"] = []
        claude_hooks["PreCompact"].append(HookMatcher(hooks=[_create_pre_compact_hook()]))

        # Register BEFORE_TOOL_USE hooks as PreToolUse matchers. The roster
        # (built-ins first, then plugin hooks) is composed by the shared
        # before_tool_use_roster so this core enforces exactly what the OpenAI
        # and claude-cli cores do.
        all_pre_tool_hooks = [
            _wrap_before_tool_use(fn) for fn in before_tool_use_roster(agnostic_hooks)
        ]

        if all_pre_tool_hooks:
            if "PreToolUse" not in claude_hooks:
                claude_hooks["PreToolUse"] = []
            # Single HookMatcher that matches all tools — hooks run in order,
            # first deny wins.
            claude_hooks["PreToolUse"].append(HookMatcher(hooks=all_pre_tool_hooks))

        # Build allowed tools list from the shared roster (cores/_tools.py).
        allowed_tools = list(BUILTIN_ALLOWED_TOOLS)

        # Add remote MCP tools if configured
        if "tools" in self.config.mcp_servers:
            allowed_tools.append("mcp__tools__*")

        # Allow tools from all configured MCP servers
        for server_name in self.config.mcp_servers:
            pattern = f"mcp__{server_name}__*"
            if pattern not in allowed_tools:
                allowed_tools.append(pattern)

        _log(f"MCP servers config: {list(self.config.mcp_servers.keys())}")
        mcp_details = {
            k: {kk: vv for kk, vv in v.items() if kk != "env"}
            for k, v in self.config.mcp_servers.items()
        }
        _log(f"MCP servers details: {json.dumps(mcp_details)}")
        _log(f"Allowed tools: {allowed_tools}")

        # Discover Claude Code plugins baked into the container image
        plugins_dir = Path("/opt/plugins")
        plugins = (
            [{"type": "local", "path": str(p)} for p in sorted(plugins_dir.iterdir()) if p.is_dir()]
            if plugins_dir.is_dir()
            else []
        )
        if plugins:
            _log(f"Loading plugins: {[p['path'] for p in plugins]}")

        # Build options
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
            mcp_servers=self.config.mcp_servers,
            hooks=claude_hooks if claude_hooks else None,
            plugins=plugins,
        )

        # Create and enter client context
        self._client = ClaudeSDKClient(options)
        await self._client.__aenter__()

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query using Claude SDK."""
        if self._client is None:
            raise RuntimeError("ClaudeAgentCore not started (call start() first)")

        _log(f"Starting query (session: {self._session_id or 'new'})...")

        await self._client.query(prompt)

        message_count = 0
        result_count = 0
        new_session_id: str | None = None

        async for message in self._client.receive_response():
            message_count += 1

            # System messages
            if isinstance(message, SystemMessage):
                if message.subtype == "init" and hasattr(message, "data"):
                    sid = message.data.get("session_id")
                    if sid:
                        new_session_id = sid
                        _log(f"Session initialized: {new_session_id}")

                yield AgentEvent(
                    type="system",
                    data={
                        "system_subtype": message.subtype,
                        "system_data": message.data if hasattr(message, "data") else {},
                    },
                )

            # Assistant messages (thinking, tool use, tool results, text)
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        yield AgentEvent(
                            type="thinking",
                            data={"thinking": block.thinking},
                        )
                    elif isinstance(block, ToolUseBlock):
                        yield AgentEvent(
                            type="tool_use",
                            data={
                                "tool_name": block.name,
                                "tool_input": block.input,
                            },
                        )
                    elif isinstance(block, ToolResultBlock):
                        # Flatten content to string for storage
                        if isinstance(block.content, str):
                            content_str = block.content
                        elif isinstance(block.content, list):
                            content_str = json.dumps(block.content)
                        else:
                            content_str = ""

                        yield AgentEvent(
                            type="tool_result",
                            data={
                                "tool_result_id": block.tool_use_id,
                                "tool_result_content": content_str,
                                "tool_result_is_error": block.is_error,
                            },
                        )
                    elif isinstance(block, TextBlock):
                        yield AgentEvent(
                            type="text",
                            data={"text": block.text},
                        )

            # Result messages
            elif isinstance(message, ResultMessage):
                result_count += 1
                text_result = getattr(message, "result", None)
                _log(
                    f"Result #{result_count}: "
                    f"subtype={message.subtype}"
                    f"{f' text={text_result[:200]}' if text_result else ''}"
                )

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

                yield AgentEvent(
                    type="result",
                    data={
                        "result": text_result,
                        "result_metadata": result_meta,
                    },
                )

        # Update session ID if we got a new one
        if new_session_id:
            self._session_id = new_session_id

        _log(f"Query done. Messages: {message_count}, results: {result_count}")

    async def stop(self) -> None:
        """Clean up Claude SDK client."""
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception as exc:
                _log(f"Error during client cleanup: {exc}")
            finally:
                self._client = None

    @property
    def session_id(self) -> str | None:
        """Return current session ID."""
        return self._session_id
