"""Tests for learning capture in the message-processing pipeline."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig, ObsidianLearningConfig
from pynchy.host.orchestrator.messaging.pipeline import MessageHandlerDeps, process_group_messages
from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

_P_SETTINGS = "pynchy.host.orchestrator.messaging.pipeline.get_settings"
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"
_P_LEARNING_START = "pynchy.host.learning.capture.start_completed_turn_learning_review"
_P_LEARNING_OBSERVE = "pynchy.host.learning.packets.observe_container_output"
_P_LEARNING_SETTINGS = "pynchy.host.learning.packets.get_settings"
_P_LEARNING_PATH_SETTINGS = "pynchy.host.learning.paths.get_settings"
_P_TEMPORAL_LEARNING_START = (
    "pynchy.host.orchestrator.temporal.scheduler.start_learning_review_workflow"
)
_P_BG_MERGE = "pynchy.host.git_ops._worktree_merge.background_merge_worktree"


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


def _settings_mock(tmp_path: Path, **overrides):
    defaults = {
        "data_dir": tmp_path,
        "learning": LearningConfig(enabled=False),
        "trigger_pattern": re.compile(r".*"),
        "idle_timeout": 300,
    }
    defaults.update(overrides)
    return make_settings(**defaults)


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
        patch(_P_SETTINGS) as settings,
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_LEARNING_START, new_callable=AsyncMock, create=True) as mock_start,
        patch(_P_BG_MERGE),
    ):
        settings.return_value = _settings_mock(tmp_path, learning=LearningConfig(enabled=True))
        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    mock_start.assert_awaited_once()
    args = mock_start.await_args.args
    assert args[:5] == (
        settings.return_value,
        "g@g.us",
        group,
        [msg],
        "new-ts",
    )
    assert args[5].final_answer == "Remembered."
    assert args[5].tool_counts == {"Bash": 1}
    assert deps.last_agent_timestamp["g@g.us"] == "new-ts"


@pytest.mark.asyncio
async def test_enabled_learning_logs_capture_attempt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    msg = _make_message("remember this", message_id="msg-42", timestamp="new-ts")
    caplog.set_level(logging.INFO)

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        if on_output:
            await on_output(ContainerOutput(status="success", type="result", result="Done."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(_P_SETTINGS) as settings,
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_LEARNING_SETTINGS) as learning_settings,
        patch(_P_LEARNING_PATH_SETTINGS) as learning_path_settings,
        patch(_P_TEMPORAL_LEARNING_START, new_callable=AsyncMock),
        patch(_P_BG_MERGE),
    ):
        enabled_settings = _settings_mock(
            tmp_path,
            learning=LearningConfig(
                enabled=True,
                obsidian=ObsidianLearningConfig(vault_root=str(tmp_path / "vault")),
            ),
        )
        settings.return_value = enabled_settings
        learning_settings.return_value = enabled_settings
        learning_path_settings.return_value = enabled_settings
        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    assert "Completed-turn learning capture finished" in caplog.text
    assert "learning-" in caplog.text


@pytest.mark.asyncio
async def test_learning_review_packet_includes_follow_up_dispatched_during_active_run(
    tmp_path: Path,
) -> None:
    group = _make_group()
    previous_cursor = "2026-07-07T09:59:59.000Z"
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": previous_cursor})
    initial = _make_message(
        "first question",
        message_id="msg-initial",
        timestamp="2026-07-07T10:00:01.000Z",
    )
    initial_tail = _make_message(
        "clarifying detail",
        message_id="msg-initial-tail",
        timestamp="2026-07-07T10:00:02.000Z",
    )
    follow_up = _make_message(
        "more context",
        message_id="msg-follow-up",
        timestamp="2026-07-07T10:00:03.000Z",
    )
    settings = _settings_mock(
        tmp_path,
        learning=LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(tmp_path / "vault")),
        ),
    )

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        deps._dispatched_through["g@g.us"] = follow_up.timestamp
        if on_output:
            await on_output(ContainerOutput(status="success", type="result", result="Remembered."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(_P_SETTINGS, return_value=settings),
        patch(_P_LEARNING_SETTINGS, return_value=settings),
        patch(_P_LEARNING_PATH_SETTINGS, return_value=settings),
        patch(_P_MSGS_SINCE, new_callable=AsyncMock) as mock_messages_since,
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_TEMPORAL_LEARNING_START, new_callable=AsyncMock) as temporal_start,
        patch(_P_BG_MERGE),
    ):
        mock_messages_since.side_effect = [[initial, initial_tail], [follow_up]]

        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    assert deps.last_agent_timestamp["g@g.us"] == follow_up.timestamp
    assert mock_messages_since.await_args_list == [
        call("g@g.us", previous_cursor),
        call("g@g.us", initial_tail.timestamp),
    ]
    temporal_start.assert_awaited_once()
    packet = temporal_start.await_args.args[0]
    assert packet.provenance["final_cursor"] == follow_up.timestamp
    assert json.loads(packet.provenance["source_message_ids"]) == [
        "msg-initial",
        "msg-initial-tail",
        "msg-follow-up",
    ]


@pytest.mark.asyncio
async def test_learning_review_is_skipped_when_expanded_fetch_fails(tmp_path: Path) -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    initial = _make_message(
        "first question",
        message_id="msg-initial",
        timestamp="2026-07-07T10:00:01Z",
    )
    follow_up_timestamp = "2026-07-07T10:00:02Z"
    settings = _settings_mock(
        tmp_path,
        learning=LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(tmp_path / "vault")),
        ),
    )

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        deps._dispatched_through["g@g.us"] = follow_up_timestamp
        if on_output:
            await on_output(ContainerOutput(status="success", type="result", result="Remembered."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(_P_SETTINGS, return_value=settings),
        patch(_P_LEARNING_SETTINGS, return_value=settings),
        patch(_P_LEARNING_PATH_SETTINGS, return_value=settings),
        patch(_P_MSGS_SINCE, new_callable=AsyncMock) as mock_messages_since,
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_TEMPORAL_LEARNING_START, new_callable=AsyncMock) as temporal_start,
        patch(_P_BG_MERGE),
    ):
        mock_messages_since.side_effect = [[initial], RuntimeError("database unavailable")]

        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    assert deps.last_agent_timestamp["g@g.us"] == follow_up_timestamp
    assert mock_messages_since.await_args_list == [
        call("g@g.us", "old-ts"),
        call("g@g.us", initial.timestamp),
    ]
    temporal_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_retryable_failure_does_not_start_learning_review(tmp_path: Path) -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    deps.run_agent = AsyncMock(return_value="error")
    msg = _make_message("hello", timestamp="new-ts")

    with (
        patch(_P_SETTINGS) as settings,
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_LEARNING_START, new_callable=AsyncMock, create=True) as mock_start,
    ):
        settings.return_value = _settings_mock(tmp_path)
        result = await process_group_messages(deps, "g@g.us")

    assert result is False
    mock_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_output_failure_does_not_start_learning_review(tmp_path: Path) -> None:
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
        patch(_P_SETTINGS) as settings,
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_LEARNING_START, new_callable=AsyncMock, create=True) as mock_start,
    ):
        settings.return_value = _settings_mock(tmp_path)
        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
    mock_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_after_turn_false_skips_follow_up_expansion_and_learning_start(
    tmp_path: Path,
) -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    initial = _make_message(
        "first question",
        message_id="msg-initial",
        timestamp="2026-07-07T10:00:01Z",
    )
    follow_up_timestamp = "2026-07-07T10:00:02Z"

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        deps._dispatched_through["g@g.us"] = follow_up_timestamp
        if on_output:
            await on_output(ContainerOutput(status="success", type="result", result="Ignored."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(
            _P_SETTINGS,
            return_value=_settings_mock(
                tmp_path,
                learning=LearningConfig(enabled=True, review_after_turn=False),
            ),
        ),
        patch(_P_MSGS_SINCE, new_callable=AsyncMock) as mock_messages_since,
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_TEMPORAL_LEARNING_START, new_callable=AsyncMock) as temporal_start,
        patch(_P_BG_MERGE),
    ):
        mock_messages_since.side_effect = [[initial], RuntimeError("should not expand")]

        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    assert deps.last_agent_timestamp["g@g.us"] == follow_up_timestamp
    assert mock_messages_since.await_count == 1
    temporal_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_learning_observation_failure_does_not_block_streamed_output(
    tmp_path: Path,
) -> None:
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
        patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
        patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_LEARNING_OBSERVE, side_effect=RuntimeError("observer broke")),
        patch(_P_TEMPORAL_LEARNING_START, new_callable=AsyncMock) as temporal_start,
        patch(_P_BG_MERGE),
    ):
        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    deps.handle_streamed_output.assert_awaited_once_with("g@g.us", group, output)
    assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
    temporal_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_learning_disabled_skips_follow_up_expansion_and_learning_start(
    tmp_path: Path,
) -> None:
    group = _make_group()
    deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
    initial = _make_message(
        "first question",
        message_id="msg-initial",
        timestamp="2026-07-07T10:00:01Z",
    )
    follow_up_timestamp = "2026-07-07T10:00:02Z"

    async def _run_agent(_group, _jid, _msgs, on_output=None, *_args, **_kwargs):
        deps._dispatched_through["g@g.us"] = follow_up_timestamp
        if on_output:
            await on_output(ContainerOutput(status="success", type="result", result="Ignored."))
        return "success"

    deps.run_agent = AsyncMock(side_effect=_run_agent)

    with (
        patch(
            _P_SETTINGS,
            return_value=_settings_mock(tmp_path, learning=LearningConfig(enabled=False)),
        ),
        patch(_P_MSGS_SINCE, new_callable=AsyncMock) as mock_messages_since,
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_TEMPORAL_LEARNING_START, new_callable=AsyncMock) as temporal_start,
        patch(_P_BG_MERGE),
    ):
        mock_messages_since.side_effect = [[initial], RuntimeError("should not expand")]

        result = await process_group_messages(deps, "g@g.us")

    assert result is True
    assert deps.last_agent_timestamp["g@g.us"] == follow_up_timestamp
    assert mock_messages_since.await_count == 1
    temporal_start.assert_not_awaited()
