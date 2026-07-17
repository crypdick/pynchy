"""Pynchy-owned values parsed from Claude Agent SDK stream messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass(frozen=True, slots=True)
class ClaudeSystemEvent:
    """A system event emitted by the Claude Agent SDK."""

    subtype: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClaudeThinkingBlock:
    """One parsed thinking block."""

    thinking: str


@dataclass(frozen=True, slots=True)
class ClaudeToolUseBlock:
    """One parsed tool-use block."""

    name: str
    input: object


@dataclass(frozen=True, slots=True)
class ClaudeToolResultBlock:
    """One parsed tool-result block."""

    tool_use_id: str
    content: object
    is_error: bool


@dataclass(frozen=True, slots=True)
class ClaudeTextBlock:
    """One parsed text block."""

    text: str


ClaudeContentBlock = (
    ClaudeThinkingBlock | ClaudeToolUseBlock | ClaudeToolResultBlock | ClaudeTextBlock
)


@dataclass(frozen=True, slots=True)
class ClaudeAssistantEvent:
    """A parsed assistant message with Pynchy-owned content blocks."""

    content: tuple[ClaudeContentBlock, ...]


@dataclass(frozen=True, slots=True)
class ClaudeResultEvent:
    """A parsed terminal result message from the Claude Agent SDK."""

    subtype: str
    duration_ms: int | None
    duration_api_ms: int | None
    is_error: bool
    num_turns: int | None
    session_id: str | None
    total_cost_usd: float | None
    usage: object
    result: str | None


ClaudeSdkEvent = ClaudeSystemEvent | ClaudeAssistantEvent | ClaudeResultEvent


def _parse_content_block(raw_block: object) -> ClaudeContentBlock | None:
    if isinstance(raw_block, ThinkingBlock):
        return ClaudeThinkingBlock(thinking=raw_block.thinking)
    if isinstance(raw_block, ToolUseBlock):
        return ClaudeToolUseBlock(name=raw_block.name, input=raw_block.input)
    if isinstance(raw_block, ToolResultBlock):
        return ClaudeToolResultBlock(
            tool_use_id=raw_block.tool_use_id,
            content=raw_block.content,
            is_error=bool(raw_block.is_error),
        )
    if isinstance(raw_block, TextBlock):
        return ClaudeTextBlock(text=raw_block.text)
    return None


def parse_claude_sdk_event(raw_message: object) -> ClaudeSdkEvent | None:
    """Parse one Claude SDK event at the agent-core boundary."""
    if isinstance(raw_message, SystemMessage):
        raw_data = getattr(raw_message, "data", {})
        return ClaudeSystemEvent(
            subtype=raw_message.subtype,
            data=raw_data if isinstance(raw_data, dict) else {},
        )
    if isinstance(raw_message, AssistantMessage):
        return ClaudeAssistantEvent(
            content=tuple(
                parsed
                for block in raw_message.content
                if (parsed := _parse_content_block(block)) is not None
            )
        )
    if isinstance(raw_message, ResultMessage):
        result = getattr(raw_message, "result", None)
        return ClaudeResultEvent(
            subtype=raw_message.subtype,
            duration_ms=raw_message.duration_ms,
            duration_api_ms=raw_message.duration_api_ms,
            is_error=raw_message.is_error,
            num_turns=raw_message.num_turns,
            session_id=raw_message.session_id,
            total_cost_usd=raw_message.total_cost_usd,
            usage=raw_message.usage,
            result=result if isinstance(result, str) else None,
        )
    return None
