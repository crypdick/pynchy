"""Typed protocol shared by agent-core producers and runner consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping


@dataclass(frozen=True)
class ResultMetadata:
    """The stable terminal metadata sent over the runner wire protocol."""

    subtype: str
    is_error: bool
    session_id: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return the result-metadata wire shape."""
        return {
            "subtype": self.subtype,
            "is_error": self.is_error,
            "session_id": self.session_id,
            **self.extra,
        }


@dataclass(frozen=True)
class ThinkingEvent:
    thinking: str
    type: Literal["thinking"] = field(default="thinking", init=False)


@dataclass(frozen=True)
class ToolUseEvent:
    tool_name: str
    tool_input: Mapping[str, object]
    type: Literal["tool_use"] = field(default="tool_use", init=False)


@dataclass(frozen=True)
class ToolResultEvent:
    tool_result_id: str
    tool_result_content: str
    tool_result_is_error: bool
    type: Literal["tool_result"] = field(default="tool_result", init=False)


@dataclass(frozen=True)
class TextEvent:
    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class SystemEvent:
    system_subtype: str
    system_data: Mapping[str, object]
    type: Literal["system"] = field(default="system", init=False)


@dataclass(frozen=True)
class ResultEvent:
    result: str | None
    result_metadata: ResultMetadata
    type: Literal["result"] = field(default="result", init=False)


type AgentEvent = (
    ThinkingEvent | ToolUseEvent | ToolResultEvent | TextEvent | SystemEvent | ResultEvent
)

_EVENT_TYPES = (ThinkingEvent, ToolUseEvent, ToolResultEvent, TextEvent, SystemEvent, ResultEvent)


def error_result(message: str, *, subtype: str) -> ResultEvent:
    """Create a terminal error that remains valid on the normal result wire path."""
    return ResultEvent(
        result=message,
        result_metadata=ResultMetadata(subtype=subtype, is_error=True),
    )


async def validate_agent_stream(events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
    """Yield only one valid terminal result, after its producer reaches EOF.

    Holding the result prevents a provider from making a completed turn look
    successful before a duplicate or post-result event proves that stream
    invalid.  Cancellation intentionally propagates unchanged.
    """
    terminal: ResultEvent | None = None
    async for event in events:
        if not isinstance(event, _EVENT_TYPES):
            yield error_result("Agent stream emitted an unknown event", subtype="protocol_error")
            return
        if terminal is not None:
            detail = (
                "duplicate terminal result"
                if isinstance(event, ResultEvent)
                else "event after terminal result"
            )
            yield error_result(f"Agent stream emitted {detail}", subtype="protocol_error")
            return
        if isinstance(event, ResultEvent):
            terminal = event
        else:
            yield event

    if terminal is None:
        yield error_result("Agent stream ended without a terminal result", subtype="missing_result")
    else:
        yield terminal
