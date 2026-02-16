"""Claude SDK agent core implementation."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import AsyncIterator
from datetime import datetime
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
from ..hooks import AGNOSTIC_TO_CLAUDE, load_hooks


def _log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[claude-core] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Transcript archival helpers (PreCompact hook)
# ---------------------------------------------------------------------------


def _sanitize_filename(summary: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:50]


def _generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def _parse_transcript(content: str) -> list[dict[str, str]]:
    """Parse JSONL transcript to messages."""
    messages: list[dict[str, str]] = []

    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                raw = entry["message"]["content"]
                text = raw if isinstance(raw, str) else "".join(c.get("text", "") for c in raw)
                if text:
                    messages.append({"role": "user", "content": text})
            elif entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                text_parts = [
                    c.get("text", "")
                    for c in entry["message"]["content"]
                    if c.get("type") == "text"
                ]
                text = "".join(text_parts)
                if text:
                    messages.append({"role": "assistant", "content": text})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    return messages


def _format_transcript_markdown(messages: list[dict[str, str]], title: str | None = None) -> str:
    """Format parsed messages as markdown."""
    now = datetime.now()
    formatted_date = now.strftime("%b %d, %I:%M %p")

    lines = [
        f"# {title or 'Conversation'}",
        "",
        f"Archived: {formatted_date}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        sender = "User" if msg["role"] == "user" else "Pynchy"
        content = msg["content"][:2000] + "..." if len(msg["content"]) > 2000 else msg["content"]
        lines.append(f"**{sender}**: {content}")
        lines.append("")

    return "\n".join(lines)


def _get_session_summary(session_id: str, transcript_path: str) -> str | None:
    """Look up session summary from sessions-index.json."""
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"

    if not index_path.exists():
        _log(f"Sessions index not found at {index_path}")
        return None

    try:
        index = json.loads(index_path.read_text())
        for entry in index.get("entries", []):
            if entry.get("sessionId") == session_id:
                return entry.get("summary")
    except Exception as exc:
        _log(f"Failed to read sessions index: {exc}")

    return None


def _create_pre_compact_hook():
    """Create a PreCompact hook that archives the transcript."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        transcript_path = input_data.get("transcript_path", "")
        session_id = input_data.get("session_id", "")

        if not transcript_path or not Path(transcript_path).exists():
            _log("No transcript found for archiving")
            return {}

        try:
            content = Path(transcript_path).read_text()
            messages = _parse_transcript(content)

            if not messages:
                _log("No messages to archive")
                return {}

            summary = _get_session_summary(session_id, transcript_path)
            name = _sanitize_filename(summary) if summary else _generate_fallback_name()

            conversations_dir = Path("/workspace/group/conversations")
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = datetime.now().strftime("%Y-%m-%d")
            filename = f"{date}-{name}.md"
            file_path = conversations_dir / filename

            markdown = _format_transcript_markdown(messages, summary)
            file_path.write_text(markdown)

            _log(f"Archived conversation to {file_path}")
        except Exception as exc:
            _log(f"Failed to archive transcript: {exc}")

        return {}

    return hook


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
            if event in AGNOSTIC_TO_CLAUDE:
                claude_hook_name = AGNOSTIC_TO_CLAUDE[event]
                if funcs:
                    claude_hooks[claude_hook_name] = [HookMatcher(hooks=[func]) for func in funcs]

        # Add built-in PreCompact hook for transcript archival
        if "PreCompact" not in claude_hooks:
            claude_hooks["PreCompact"] = []
        claude_hooks["PreCompact"].append(HookMatcher(hooks=[_create_pre_compact_hook()]))

        # Build options
        options = ClaudeAgentOptions(
            model="opus",
            cwd=self.config.cwd,
            resume=self.config.session_id,
            system_prompt=system_prompt,
            allowed_tools=[
                "Bash",
                "BashOutput",
                "KillBash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
                "Task",
                "TaskOutput",
                "TaskStop",
                "TeamCreate",
                "TeamDelete",
                "SendMessage",
                "TodoWrite",
                "ToolSearch",
                "Skill",
                "NotebookEdit",
                "mcp__pynchy__*",
            ],
            permission_mode="bypassPermissions",
            settings='{"attribution": {"commit": "", "pr": ""}}',
            setting_sources=["project", "user"],
            mcp_servers=self.config.mcp_servers,
            hooks=claude_hooks if claude_hooks else None,
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
