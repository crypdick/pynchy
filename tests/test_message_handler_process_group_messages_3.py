"""Tests for message processing (message_handler) and routing (_message_routing).

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, advance_cursor, and reset handoff behavior
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import (
    ContainerOutput,
    InFlightWorkKind,
)
from pynchy.host.orchestrator.messaging.pipeline import (
    process_group_messages,
)
from pynchy.state import (
    get_in_flight_turn_for_chat,
    init_test_database,
    prepare_in_flight_turn_recovery,
)
from pynchy.turn_outcomes import TurnOutcome
from tests.message_handler_support import (
    _dirty_notice_present,
    _make_deps,
    _make_group,
    _make_message,
    _observe_at_run,
    _patch_fmt_sdk,
    _patch_intercept,
    _patch_msgs_since,
    _reset_file,
    _run_loop_once,
    _run_with_observer,
)

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = f"{_PR}.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestProcessGroupMessages:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_missing_terminal_result_keeps_checkpoint_for_recovery(self, tmp_path):
        """Text and tools alone never acknowledge the inbound turn."""
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        msg = _make_message("finish SYN-36", chat_jid=jid, timestamp="new-ts")

        async def resultless_run(_group, _jid, _messages, on_output=None, *_args, **_kwargs):
            assert on_output is not None
            await on_output(ContainerOutput(status="success", type="text", text="working"))
            await on_output(
                ContainerOutput(
                    status="success",
                    type="tool_result",
                    tool_result_id="tool-1",
                    tool_result_content="done",
                )
            )
            await on_output(
                ContainerOutput(
                    status="error",
                    result_metadata={"subtype": "missing_terminal_turn"},
                )
            )
            return "error"

        deps.run_agent = AsyncMock(side_effect=resultless_run)
        deps.handle_streamed_output = AsyncMock(return_value=False)

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            assert await process_group_messages(deps, jid) is TurnOutcome.RETRY

        checkpoint = await get_in_flight_turn_for_chat(jid, {InFlightWorkKind.INTERACTIVE})
        assert checkpoint is not None
        assert checkpoint.claimed_at is None
        assert deps.last_agent_timestamp[jid] == "old-ts"
        deps.broadcast_host_message.assert_not_awaited()

        recovered = await prepare_in_flight_turn_recovery("deploy-sha")
        assert [turn.turn_id for turn in recovered] == [checkpoint.turn_id]

    @pytest.mark.asyncio
    async def test_agent_error_after_output_sent_no_rollback(self, tmp_path):
        """Agent error after output was sent → no rollback."""
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={"g@g.us": group},
            last_agent_ts={"g@g.us": "old-ts"},
        )
        msg = _make_message("hello", timestamp="new-ts")

        # run_agent invokes the on_output callback to simulate
        # output being sent before error.
        async def mock_run_agent(group, jid, msgs, on_output=None, notices=None, **_kwargs):
            if on_output:
                output = ContainerOutput(type="result", result="hello", status="error")
                await on_output(output)
            return "error"

        deps.run_agent = AsyncMock(side_effect=mock_run_agent)
        deps.handle_streamed_output = AsyncMock(return_value=True)

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
        deps.broadcast_host_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dirty_repo_warning_added_for_admin_group(self, tmp_path):
        """Dirty repo after reset → system notice added."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        dirty_check = ipc_dir / "needs_dirty_check.json"
        dirty_check.write_text(json.dumps({"timestamp": "2024-01-01T00:00:00Z"}))
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            deps.repo_is_dirty.return_value = True
            await process_group_messages(deps, "g@g.us")

        call_args = deps.run_agent.call_args
        # system_notices is the 5th positional arg
        notices = (
            call_args[0][4] if len(call_args[0]) > 4 else call_args[1].get("extra_system_notices")
        )
        assert notices is not None
        assert any("uncommitted" in n.lower() for n in notices)
        assert not dirty_check.exists()

    @pytest.mark.asyncio
    async def test_reaction_and_typing_indicator_sent(self, tmp_path):
        """Processing messages sends reaction and typing indicator."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts", message_id="msg-42")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_awaited_once_with(
            "g@g.us", "msg-42", msg.sender, "🦞"
        )
        assert deps.set_typing_on_channels.await_count == 2
        deps.set_typing_on_channels.assert_any_await("g@g.us", is_typing=True)
        deps.set_typing_on_channels.assert_any_await("g@g.us", is_typing=False)

    @pytest.mark.asyncio
    async def test_custom_processing_ack_emoji_used_when_configured(self, tmp_path):
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        deps.processing_ack_emoji.return_value = "👀"
        msg = _make_message("hello", timestamp="new-ts", message_id="msg-42")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_awaited_once_with(
            "g@g.us", "msg-42", msg.sender, "👀"
        )

    @pytest.mark.asyncio
    async def test_processing_ack_skipped_when_channel_disables_it(self, tmp_path):
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        deps.processing_ack_emoji.return_value = None
        msg = _make_message("hello", timestamp="new-ts", message_id="msg-42")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_only_does_not_launch_agent(self, tmp_path):
        """System notices alone shouldn't launch a container."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        notice = _make_message(
            "[System Notice] Auto-rebased 1 commit(s) onto your worktree.",
            sender="system_notice",
            sender_name="System",
            timestamp="new-ts",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([notice]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_plus_user_message_launches_agent(self, tmp_path):
        """A mix of system notices and user messages should launch the agent."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)

        notice = _make_message(
            "[System Notice] Auto-rebased 1 commit(s) onto your worktree.",
            message_id="notice-1",
            sender="system_notice",
            sender_name="System",
            timestamp="ts-1",
        )
        user_msg = _make_message(
            "hello",
            message_id="msg-1",
            sender="user@s.whatsapp.net",
            sender_name="Alice",
            timestamp="ts-2",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([notice, user_msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_special_command_intercepts(self, tmp_path):
        """Special commands checked on the last message."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        msg2 = _make_message("reset context", timestamp="ts-2")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg2]),
            _patch_intercept(return_value=True),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_zzz_reaction_registered_on_session_idle_callback(self, tmp_path):
        """After a successful agent run that produced output, the zzz
        reaction should be registered as the session's idle callback
        (fired when the container actually hibernates, not immediately)."""
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})

        msg = _make_message("hello", message_id="msg-42", timestamp="new-ts")

        # run_agent must invoke the on_output callback so output_sent_to_user
        # is set to True inside process_group_messages.
        fake_result = ContainerOutput(status="success", result="done")

        async def _run_agent_with_callback(_group, _jid, _msgs, on_output=None, *a, **kw):
            if on_output:
                await on_output(fake_result)
            return "success"

        deps.run_agent = AsyncMock(side_effect=_run_agent_with_callback)
        deps.handle_streamed_output = AsyncMock(return_value=True)
        deps.send_reaction_to_outbound = AsyncMock()

        fake_ids = {"slack": "1234567890.000001"}
        registered_idle_callback: object | None = None

        def register_idle_callback(_group_folder, callback) -> None:
            nonlocal registered_idle_callback
            registered_idle_callback = callback

        deps.register_idle_callback = MagicMock(side_effect=register_idle_callback)

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.pop_last_result_ids",
                return_value=fake_ids,
            ),
        ):
            result = await process_group_messages(deps, jid)

        assert result is TurnOutcome.COMPLETED
        # The reaction should NOT be sent immediately
        deps.send_reaction_to_outbound.assert_not_awaited()
        # Instead, the runtime operation should register an idle callback.
        deps.register_idle_callback.assert_called_once()

        # Calling the stored callback should send the zzz reaction
        assert registered_idle_callback is not None
        callback = registered_idle_callback
        await callback()
        deps.send_reaction_to_outbound.assert_awaited_once_with(jid, fake_ids, "zzz")


class TestCheckDirtyRepo:
    """After a context reset the pipeline drops a needs_dirty_check.json marker.

    On the next run process_group_messages checks the repo and, if dirty,
    prepends an 'uncommitted changes' system notice for the agent. The marker
    is always consumed, and a check failure must not crash the run.
    """

    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_no_marker_no_notice(self, tmp_path):
        """No marker file → no dirty notice passed to the agent."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            await process_group_messages(deps, "g@g.us")

        assert not _dirty_notice_present(deps)

    @pytest.mark.asyncio
    async def test_clean_repo_no_notice_marker_consumed(self, tmp_path):
        """Marker file + clean repo → no notice, marker consumed."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        marker = tmp_path / "ipc" / "test-group" / "needs_dirty_check.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            await process_group_messages(deps, "g@g.us")

        assert not _dirty_notice_present(deps)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_dirty_repo_adds_notice_marker_consumed(self, tmp_path):
        """Marker file + dirty repo → warning notice, marker consumed."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        marker = tmp_path / "ipc" / "test-group" / "needs_dirty_check.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            deps.repo_is_dirty.return_value = True
            await process_group_messages(deps, "g@g.us")

        assert _dirty_notice_present(deps)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_oserror_during_check_consumes_marker(self, tmp_path):
        """OSError during the dirty check → no crash, marker consumed, no notice."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        marker = tmp_path / "ipc" / "test-group" / "needs_dirty_check.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            deps.repo_is_dirty.side_effect = OSError("permission denied")
            # Should not raise
            await process_group_messages(deps, "g@g.us")

        assert not _dirty_notice_present(deps)
        assert not marker.exists()


class TestMarkDispatched:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_records_dispatched_timestamp(self, tmp_path):
        """The furthest message timestamp is recorded in-memory before the run."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        observed = _observe_at_run(deps)

        await _run_with_observer(tmp_path, deps)

        assert observed["dispatched"] == "new-ts"

    @pytest.mark.asyncio
    async def test_does_not_advance_cursor_before_completion(self, tmp_path):
        """last_agent_timestamp is untouched at dispatch — it advances only after."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        observed = _observe_at_run(deps)

        await _run_with_observer(tmp_path, deps)

        assert observed["cursor"] == "old-ts"

    @pytest.mark.asyncio
    async def test_does_not_save_state_before_run(self, tmp_path):
        """Dispatch tracking is in-memory only — no DB write before the run."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        observed = _observe_at_run(deps)

        await _run_with_observer(tmp_path, deps)

        assert observed["saves"] == 0


class TestHandleResetHandoff:
    """process_group_messages consumes an agent-written reset_prompt.json before
    handling normal traffic: a valid prompt runs a handoff turn, an empty/absent
    prompt falls through, a malformed prompt is discarded, and a handoff error
    signals GroupQueue to retry the turn.
    """

    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_no_reset_file_processes_normally(self, tmp_path):
        """No reset_prompt.json → falls through to normal message processing."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_reset_runs_handoff(self, tmp_path):
        """Valid reset prompt → handoff agent runs, file consumed."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        reset_file = _reset_file(tmp_path)
        reset_file.write_text(json.dumps({"message": "Continue after reset"}))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_awaited_once()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_empty_message_skips_agent(self, tmp_path):
        """Empty reset message → no handoff run, file consumed, run continues."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        reset_file = _reset_file(tmp_path)
        reset_file.write_text(json.dumps({"message": ""}))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_malformed_json_falls_through(self, tmp_path):
        """Malformed reset_prompt.json → discarded, normal processing proceeds."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        reset_file = _reset_file(tmp_path)
        reset_file.write_text("NOT VALID JSON")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_handoff_agent_error_signals_retry(self, tmp_path):
        """Handoff agent returning 'error' requests a retry."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.run_agent = AsyncMock(return_value="error")
        reset_file = _reset_file(tmp_path)
        reset_file.write_text(json.dumps({"message": "Hello"}))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.RETRY


@pytest.mark.asyncio
async def test_message_loop_does_not_run_channel_reconciliation_locally():
    """Channel reconciliation is Temporal-scheduled, not message-loop work."""
    deps = _make_deps()
    deps.catch_up_channels = AsyncMock()

    with (
        patch(_PR_NEW_MSGS, new_callable=AsyncMock, return_value=([], "")),
    ):
        await _run_loop_once(deps)

    deps.catch_up_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_loop_does_not_start_agent_for_host_only_messages():
    deps = _make_deps(groups={"group@g.us": _make_group()})
    host_message = _make_message("host notice", message_id="host-only")
    host_message.message_type = "host"

    with (
        patch(
            _PR_NEW_MSGS,
            new_callable=AsyncMock,
            return_value=([host_message], "poll-ts"),
        ) as get_new_messages,
        patch(_PR_MSGS_SINCE, new_callable=AsyncMock, return_value=[host_message]) as get_messages,
        patch(
            "pynchy.host.orchestrator.messaging.inbound.intercept_immediate_checkpoint_controls",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
    ):
        await _run_loop_once(deps)

    deps.run_agent.assert_not_awaited()
    get_new_messages.assert_awaited_once_with(["group@g.us"], "")
    get_messages.assert_awaited_once()
