"""Tests for learning capture in the message-processing pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.learning.api import capture as learning_capture
from pynchy.host.orchestrator.messaging.pipeline import MessageHandlerDeps, process_group_messages
from pynchy.plugins.api import NewMessage
from pynchy.state import init_test_database
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile

_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"
_P_LEARNING_OBSERVE = "pynchy.host.learning.packets.observe_container_output"


@pytest.fixture(autouse=True)
async def _isolated_turn_ledger() -> None:
    await init_test_database()


def _make_deps(
    *,
    groups: dict[str, WorkspaceProfile] | None = None,
    last_agent_ts: dict[str, str] | None = None,
) -> MagicMock:
    deps = MagicMock(spec=MessageHandlerDeps)
    deps.workspaces = groups or {}
    deps.last_agent_timestamp = last_agent_ts if last_agent_ts is not None else {}
    deps._dispatched_through = {}
    deps.channels = []
    deps.last_timestamp = ""
    deps.message_data_dir = Path()
    deps.routing_cursor = MagicMock(
        side_effect=lambda jid: max(
            deps.last_agent_timestamp.get(jid, ""),
            deps._dispatched_through.get(jid, ""),
        )
    )
    deps.mark_dispatched = MagicMock(side_effect=deps._dispatched_through.__setitem__)
    deps.pop_dispatched = MagicMock(side_effect=deps._dispatched_through.pop)

    deps.save_state = AsyncMock()
    deps.handle_context_reset = AsyncMock()
    deps.handle_end_session = AsyncMock()
    deps.trigger_manual_redeploy = AsyncMock()
    deps.broadcast_to_channels = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    deps.send_reaction_to_channels = AsyncMock()
    deps.send_reaction_to_outbound = AsyncMock()
    deps.set_typing_on_channels = AsyncMock()
    deps.catch_up_channels = AsyncMock()
    deps.emit = MagicMock()
    deps.repo_is_dirty = MagicMock(return_value=False)
    deps.new_learning_run_summary = learning_capture.LearningRunSummary
    deps.observe_learning_output = learning_capture.observe_learning_output
    deps.start_completed_turn_learning_review = AsyncMock()
    deps.run_agent = AsyncMock(return_value="success")
    deps.handle_streamed_output = AsyncMock(return_value=False)

    deps.queue = MagicMock()
    deps.queue.is_active_task = MagicMock(return_value=False)
    deps.queue.send_message = MagicMock(return_value=False)
    deps.queue.enqueue_message_check = MagicMock()
    deps.queue.clear_pending_tasks = MagicMock()
    deps.queue.stop_active_process = AsyncMock()
    deps.queue.close_stdin = MagicMock()
    return deps


def _make_group(
    *,
    name: str = "test-group",
    folder: str = "test-group",
    is_admin: bool = True,
) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="g@g.us",
        name=name,
        folder=folder,
        trigger="@pynchy",
        is_admin=is_admin,
    )


def _make_message(
    content: str = "hello",
    *,
    message_id: str = "msg-1",
    timestamp: str = "2024-01-01T00:00:01.000Z",
) -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid="g@g.us",
        sender="user@s.whatsapp.net",
        sender_name="Alice",
        content=content,
        timestamp=timestamp,
    )


def _patch_intercept(*, return_value: bool = False):
    return patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=return_value)


def _patch_fmt_sdk():
    return patch(_P_FMT_SDK, return_value=[{"content": "hello"}])


@pytest.mark.asyncio
async def test_clean_successful_turn_starts_temporal_learning_review_after_cursor(
    tmp_path: Path,
) -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    msg = _make_message("what should we remember?", message_id="msg-42", timestamp="new-ts")

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        if on_output:
            await on_output(ContainerOutput(status="success", type="tool_use", tool_name="Bash"))
            await on_output(ContainerOutput(status="success", type="result", result="Remembered."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        result = await process_group_messages(deps, "g@g.us")

    assert result is TurnOutcome.COMPLETED
    deps.start_completed_turn_learning_review.assert_awaited_once()
    args = deps.start_completed_turn_learning_review.await_args.args
    assert args[:4] == (
        "g@g.us",
        group,
        [msg],
        "new-ts",
    )
    assert args[4].final_answer == "Remembered."
    assert args[4].tool_counts == {"Bash": 1}
    assert deps.last_agent_timestamp["g@g.us"] == "new-ts"


@pytest.mark.asyncio
async def test_completed_turn_learning_is_delegated() -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    msg = _make_message("remember this", message_id="msg-42", timestamp="new-ts")

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        if on_output:
            await on_output(ContainerOutput(status="success", type="result", result="Done."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        result = await process_group_messages(deps, "g@g.us")

    assert result is TurnOutcome.COMPLETED
    deps.start_completed_turn_learning_review.assert_awaited_once()


@pytest.mark.asyncio
async def test_retryable_failure_does_not_start_learning_review() -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    deps.run_agent = AsyncMock(return_value="error")
    msg = _make_message("hello", timestamp="new-ts")

    with (
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        result = await process_group_messages(deps, "g@g.us")

    assert result is TurnOutcome.RETRY
    deps.start_completed_turn_learning_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_output_failure_does_not_start_learning_review() -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    msg = _make_message("hello", timestamp="new-ts")

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        if on_output:
            await on_output(ContainerOutput(status="error", type="result", result="partial error"))
        return "error"

    deps.run_agent = AsyncMock(side_effect=_run_agent)
    deps.handle_streamed_output = AsyncMock(return_value=True)

    with (
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        result = await process_group_messages(deps, "g@g.us")

    assert result is TurnOutcome.COMPLETED
    assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
    deps.start_completed_turn_learning_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_learning_observation_failure_does_not_block_streamed_output() -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    msg = _make_message("hello", timestamp="new-ts")
    output = ContainerOutput(status="success", type="result", result="Visible answer")

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        if on_output:
            await on_output(output)
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)
    deps.handle_streamed_output = AsyncMock(return_value=True)

    with (
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_LEARNING_OBSERVE, side_effect=RuntimeError("observer broke")),
    ):
        result = await process_group_messages(deps, "g@g.us")

    assert result is TurnOutcome.COMPLETED
    deps.handle_streamed_output.assert_awaited_once()
    args = deps.handle_streamed_output.await_args
    assert args.args == ("g@g.us", group, output)
    assert args.kwargs["turn_id"].startswith("turn_")
    assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
    deps.start_completed_turn_learning_review.assert_awaited_once()
