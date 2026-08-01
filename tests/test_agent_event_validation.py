"""Public validation of the agent runner event stream contract."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.events import (
    ResultEvent,
    ResultMetadata,
    TextEvent,
    ToolUseEvent,
    validate_agent_stream,
)


async def _events(*events: object):
    await asyncio.sleep(0)
    for event in events:
        yield event


async def _validated(*events: object) -> list[object]:
    return [event async for event in validate_agent_stream(_events(*events))]


@pytest.mark.asyncio
async def test_stream_without_a_terminal_result_gets_a_protocol_error() -> None:
    result = await _validated(TextEvent("partial"))

    assert result == [
        TextEvent("partial"),
        ResultEvent(
            "Agent stream ended without a terminal result",
            ResultMetadata(subtype="missing_result", is_error=True),
        ),
    ]


@pytest.mark.asyncio
async def test_unknown_event_stops_stream_with_a_protocol_error() -> None:
    result = await _validated(object())

    assert result == [
        ResultEvent(
            "Agent stream emitted an unknown event",
            ResultMetadata(subtype="protocol_error", is_error=True),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_terminal",
    [ResultEvent("second", ResultMetadata("done", False)), TextEvent("late")],
)
async def test_events_after_a_terminal_result_are_rejected(after_terminal: object) -> None:
    result = await _validated(
        ResultEvent("done", ResultMetadata("done", False)),
        after_terminal,
    )

    detail = (
        "duplicate terminal result"
        if isinstance(after_terminal, ResultEvent)
        else "event after terminal result"
    )
    assert result == [
        ResultEvent(
            f"Agent stream emitted {detail}",
            ResultMetadata(subtype="protocol_error", is_error=True),
        )
    ]


@pytest.mark.asyncio
async def test_valid_stream_preserves_nonterminal_events_and_terminal_result() -> None:
    terminal = ResultEvent("done", ResultMetadata("done", False, session_id="s1"))

    result = await _validated(ToolUseEvent("search", {"q": "pynchy"}), terminal)

    assert result == [ToolUseEvent("search", {"q": "pynchy"}), terminal]
