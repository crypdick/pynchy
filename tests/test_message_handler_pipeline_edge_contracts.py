"""Public message-pipeline contracts for unusual turn boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import ContainerOutput, InFlightWorkKind
from pynchy.host.orchestrator.messaging.cursor import (
    complete_turn_with_cursor as persist_completed_turn,
)
from pynchy.host.orchestrator.messaging.pipeline import (
    process_group_messages,
    run_queued_message_turn,
)
from pynchy.state import get_in_flight_turn_for_chat, init_test_database
from pynchy.turn_outcomes import TurnOutcome
from tests.message_handler_support import (
    _make_deps,
    _make_group,
    _make_message,
    _patch_fmt_sdk,
    _patch_intercept,
    _patch_msgs_since,
)


@pytest.fixture(autouse=True)
async def _isolated_turn_ledger() -> None:
    await init_test_database()


@pytest.mark.asyncio
async def test_preserves_supplied_turn_id_when_terminal_output_is_missing(tmp_path) -> None:
    jid = "g@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
    message = _make_message(
        timestamp="new-ts",
        metadata={"turn_id": "turn-from-input"},
    )

    async def resultless_run(_group, _jid, _messages, on_output, *_args, **_kwargs):
        await on_output(ContainerOutput(status="success", type="text", text="working"))
        return "success"

    deps.run_agent = AsyncMock(side_effect=resultless_run)
    with (
        patch.object(deps, "message_data_dir", tmp_path),
        _patch_msgs_since([message]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        assert await process_group_messages(deps, jid) is TurnOutcome.RETRY

    checkpoint = await get_in_flight_turn_for_chat(jid, {InFlightWorkKind.INTERACTIVE})
    assert checkpoint is not None
    assert checkpoint.turn_id == "turn-from-input"


@pytest.mark.asyncio
async def test_rejects_one_turn_with_multiple_delivery_claims(tmp_path) -> None:
    jid = "g@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group})
    messages = [
        _make_message(timestamp="first", metadata={"conversation_claim_id": "claim-a"}),
        _make_message(
            message_id="msg-2",
            timestamp="second",
            metadata={"conversation_claim_id": "claim-b"},
        ),
    ]

    with (
        patch.object(deps, "message_data_dir", tmp_path),
        _patch_msgs_since(messages),
        _patch_intercept(),
        _patch_fmt_sdk(),
        pytest.raises(RuntimeError, match="multiple conversation delivery claims"),
    ):
        await process_group_messages(deps, jid)

    deps.run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_clears_started_turn_when_processing_announcement_fails(tmp_path) -> None:
    jid = "g@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group})
    deps.set_typing_on_channels.side_effect = RuntimeError("typing unavailable")
    message = _make_message(timestamp="new-ts")

    with (
        patch.object(deps, "message_data_dir", tmp_path),
        _patch_msgs_since([message]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        pytest.raises(RuntimeError, match="typing unavailable"),
    ):
        await process_group_messages(deps, jid)

    assert await get_in_flight_turn_for_chat(jid, {InFlightWorkKind.INTERACTIVE}) is None
    deps.run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_host_boundary_commits_its_input_cursor(tmp_path) -> None:
    jid = "g@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
    message = _make_message(timestamp="new-ts")

    async def host_run(_group, _jid, _messages, on_output, *_args, **_kwargs):
        await on_output(ContainerOutput(status="success", type="result", result="done"))
        return "success"

    deps.run_agent = AsyncMock(side_effect=host_run)
    with (
        patch.object(deps, "message_data_dir", tmp_path),
        _patch_msgs_since([message]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

    assert deps.last_agent_timestamp[jid] == "new-ts"


@pytest.mark.asyncio
async def test_advances_cursor_for_message_dispatched_during_finalization(tmp_path) -> None:
    jid = "g@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
    message = _make_message(timestamp="new-ts")

    async def complete_then_observe_late_dispatch(*args, **kwargs):
        result = await persist_completed_turn(*args, **kwargs)
        deps.mark_dispatched(jid, "z-latest-ts")
        return result

    with (
        patch.object(deps, "message_data_dir", tmp_path),
        _patch_msgs_since([message]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(
            "pynchy.host.orchestrator.messaging.pipeline.complete_turn_with_cursor",
            new=complete_then_observe_late_dispatch,
        ),
    ):
        assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

    assert deps.last_agent_timestamp[jid] == "z-latest-ts"


@pytest.mark.asyncio
async def test_queued_turn_without_workspace_completes_without_queueing() -> None:
    deps = _make_deps(groups={})

    assert await run_queued_message_turn(deps, "unknown@g.us") is TurnOutcome.COMPLETED
    deps.queue.run_message_turn.assert_not_called()
