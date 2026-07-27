"""Provider-neutral terminal stream contract tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runner.events import ResultEvent, ResultMetadata, TextEvent, validate_agent_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_runner.events import AgentEvent


async def _events(*items: object) -> AsyncIterator[AgentEvent]:
    for item in items:
        await asyncio.sleep(0)
        yield cast("AgentEvent", item)


async def _collected(*items: object) -> list[AgentEvent]:
    return [event async for event in validate_agent_stream(_events(*items))]


def _result(text: str) -> ResultEvent:
    return ResultEvent(
        result=text,
        result_metadata=ResultMetadata(subtype="result", is_error=False),
    )


@pytest.mark.asyncio
async def test_validator_turns_clean_eof_without_result_into_terminal_error() -> None:
    partial, event = await _collected(TextEvent(text="partial"))

    assert partial == TextEvent(text="partial")
    assert isinstance(event, ResultEvent)
    assert event.result_metadata.subtype == "missing_result"


@pytest.mark.asyncio
async def test_validator_rejects_duplicate_results_before_publishing_either() -> None:
    (event,) = await _collected(_result("first"), _result("second"))

    assert isinstance(event, ResultEvent)
    assert event.result_metadata.subtype == "protocol_error"
    assert event.result == "Agent stream emitted duplicate terminal result"


@pytest.mark.asyncio
async def test_validator_rejects_events_after_a_terminal_result() -> None:
    (event,) = await _collected(_result("done"), TextEvent(text="too late"))

    assert isinstance(event, ResultEvent)
    assert event.result_metadata.subtype == "protocol_error"
    assert event.result == "Agent stream emitted event after terminal result"


@pytest.mark.asyncio
async def test_validator_rejects_an_unknown_runtime_event() -> None:
    (event,) = await _collected(object())

    assert isinstance(event, ResultEvent)
    assert event.result_metadata.subtype == "protocol_error"
    assert event.result == "Agent stream emitted an unknown event"


@pytest.mark.asyncio
async def test_validator_holds_result_until_clean_end_of_stream() -> None:
    events = await _collected(TextEvent(text="partial"), _result("done"))

    assert events == [TextEvent(text="partial"), _result("done")]


@pytest.mark.asyncio
async def test_validator_preserves_cancellation() -> None:
    class CancelledStream:
        def __aiter__(self) -> CancelledStream:
            return self

        async def __anext__(self) -> AgentEvent:
            await asyncio.sleep(0)
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in validate_agent_stream(CancelledStream())]
