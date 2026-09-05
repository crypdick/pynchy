"""Tests for message processing (message_handler) and routing (_message_routing).

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, advance_cursor, and reset handoff behavior
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.identifiers import (
    RuntimeId,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import (
    begin_in_flight_turn,
    create_task,
    get_in_flight_turn,
    get_in_flight_turn_for_task,
    init_test_database,
    is_chat_paused,
    message_cursor,
    store_message,
)
from pynchy.workspace.api import (
    RuntimeTarget,
)
from tests.message_handler_support import (
    _make_deps,
    _make_group,
    _make_message,
    _run_loop_once,
)

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_arriving_with_pause_is_queued_instead_of_sent_to_dead_ipc():
    await init_test_database()
    jid = "group@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group})
    deps.queue.has_active_run.return_value = True
    deps.queue.send_message.return_value = True
    deps.queue.clear_pending_tasks.return_value = (
        "linear-execute-syn-173",
        "recurring-task",
    )
    queued_once = ScheduledTask(
        id="linear-execute-syn-173",
        group_folder=group.folder,
        chat_jid=jid,
        prompt="Execute SYN-173",
        schedule_type="once",
        schedule_value="2026-07-25T10:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        bound_group_folder=group.folder,
        bound_chat_jid=jid,
        input_source="trusted:linear:authorized",
    )
    await create_task(queued_once)
    await create_task(
        replace(queued_once, id="recurring-task", schedule_type="cron", schedule_value="0 * * * *")
    )
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-pausing-with-reply",
            chat_jid=jid,
            group_folder=group.folder,
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[{"sender_name": "Alice", "content": "original work"}],
            input_start_cursor="",
            input_end_cursor="2026-07-25T10:00:00+00:00",
            started_at="2026-07-25T10:00:00+00:00",
            claimed_at="2026-07-25T10:00:00+00:00",
        )
    )
    pause = _make_message(
        "pause",
        message_id="pause-race",
        chat_jid=jid,
        timestamp="2026-07-25T10:00:01+00:00",
    )
    guidance = _make_message(
        "Continue without publishing.",
        message_id="guidance-race",
        chat_jid=jid,
        timestamp="2026-07-25T10:00:02+00:00",
    )
    await store_message(pause)
    await store_message(guidance)

    with (
        patch(
            _PR_NEW_MSGS,
            new_callable=AsyncMock,
            return_value=([pause, guidance], guidance.timestamp),
        ),
    ):
        await _run_loop_once(deps)

    deps.queue.send_message.assert_not_called()
    deps.queue.enqueue_message_check.assert_called_once_with(
        RuntimeTarget.from_binding(group.folder, jid)
    )
    checkpoint = await get_in_flight_turn("turn-pausing-with-reply")
    assert checkpoint is not None
    assert checkpoint.control_state is CheckpointControlState.PAUSE_REQUESTED
    assert deps.last_agent_timestamp[jid] == message_cursor(pause)
    deps.queue.clear_pending_tasks.assert_called_once_with(RuntimeId(group.folder))
    queued_turn = await get_in_flight_turn_for_task("linear-execute-syn-173")
    assert queued_turn is not None
    assert queued_turn.control_state is CheckpointControlState.PAUSED
    assert await get_in_flight_turn_for_task("recurring-task") is None
    assert await is_chat_paused(jid) is True


class TestBtwNonInterruptingMessages:
    """Messages starting with 'btw' should not interrupt active tasks.

    They are forwarded via IPC (best-effort) and the group is marked for
    reprocessing after the task exits — but the task is NOT killed and the
    cursor is NOT advanced.
    """

    @pytest.fixture(autouse=True)
    def _allow_all_senders(self, monkeypatch):
        """Route sender checks through permissive test settings."""
        settings = make_settings()
        monkeypatch.setattr("pynchy.config.access.get_settings", lambda: settings)

    @pytest.mark.asyncio
    async def test_btw_message_does_not_interrupt_active_task(self):
        """A 'btw ...' message while a task runs should forward via IPC
        and mark pending, without killing the task."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # Simulate an active scheduled task
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("btw here's some extra context", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # IPC forwarded (best-effort)
        deps.queue.send_message.assert_called_once_with(
            RuntimeId(group.folder),
            "Alice: btw here's some extra context",
        )
        # Marked for reprocessing after task exits
        deps.queue.enqueue_message_check.assert_called_once_with(
            RuntimeTarget.from_binding(group.folder, jid)
        )

        # Task NOT interrupted
        deps.queue.stop_active_process.assert_not_awaited()
        deps.queue.clear_pending_tasks.assert_not_called()

        # Cursor NOT advanced
        assert deps.last_agent_timestamp.get(jid) == "old-ts"

    @pytest.mark.asyncio
    async def test_btw_case_insensitive(self):
        """'BTW ...' (uppercase) should also be non-interrupting."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("BTW also check the logs", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Still forwarded, not interrupted
        deps.queue.send_message.assert_called_once()
        deps.queue.enqueue_message_check.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()
        deps.queue.clear_pending_tasks.assert_not_called()

    @pytest.mark.asyncio
    async def test_btw_with_leading_whitespace(self):
        """'  btw ...' with leading whitespace should be non-interrupting
        (content is stripped before prefix check)."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("  btw one more thing", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.send_message.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_btw_message_defers_interrupt_until_tool_result(self):
        """A regular message queues a boundary interruption and clears pending tasks."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True

        msg = _make_message("do something else now", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.clear_pending_tasks.assert_called_once_with(RuntimeId(group.folder))
        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(
            RuntimeId(group.folder)
        )
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_btw_without_space_defers_interrupt_until_tool_result(self):
        """'btwsomething' queues the same boundary interruption as normal input."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True

        msg = _make_message("btwsomething", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.clear_pending_tasks.assert_called_once_with(RuntimeId(group.folder))
        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(
            RuntimeId(group.folder)
        )
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_btw_only_checked_on_last_message(self):
        """When multiple messages are pending, only the last one's content
        determines whether the batch is 'btw' (non-interrupting) or not."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg1 = _make_message(
            "do something urgent",
            message_id="msg-1",
            timestamp="ts-1",
        )
        msg2 = _make_message(
            "btw also consider this",
            message_id="msg-2",
            timestamp="ts-2",
        )

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg1, msg2], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg1, msg2],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Last message starts with "btw " → non-interrupting path
        deps.queue.send_message.assert_called_once()
        deps.queue.enqueue_message_check.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()
        deps.queue.clear_pending_tasks.assert_not_called()

        # Formatted text sent to IPC should include both messages
        ipc_text = deps.queue.send_message.call_args[0][1]
        assert "do something urgent" in ipc_text
        assert "btw also consider this" in ipc_text

    @pytest.mark.asyncio
    async def test_btw_non_interrupting_during_message_processing(self):
        """'btw ...' while the agent is processing messages (not a task)
        should forward via IPC but not advance the cursor — the message
        is queued for reprocessing after the agent's turn ends."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # Not a scheduled task, but a message container IS active
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = True  # container is active

        msg = _make_message("btw here's some info", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # IPC forwarded
        deps.queue.send_message.assert_called_once()
        # Marked for reprocessing after agent turn ends
        deps.queue.enqueue_message_check.assert_called_once_with(
            RuntimeTarget.from_binding(group.folder, jid)
        )
        # Cursor NOT advanced
        assert deps.last_agent_timestamp.get(jid) == "old-ts"
        # No reaction sent (non-interrupting, will be reprocessed)
        deps.send_reaction_to_channels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_only_does_not_wake_sleeping_agent(self):
        """System notices alone shouldn't enqueue a message check when
        no container is active — the agent stays asleep."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        notice = _make_message(
            "[System Notice] Auto-rebased 3 commit(s) onto your worktree.",
            sender="system_notice",
            sender_name="System",
            timestamp="new-ts",
        )

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([notice], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[notice],
            ),
        ):
            await _run_loop_once(deps)

        # Agent NOT woken up
        deps.queue.enqueue_message_check.assert_not_called()
        deps.queue.send_message.assert_not_called()
        # Cursor NOT advanced (notice will be included in next real session)
        assert deps.last_agent_timestamp.get(jid) == "old-ts"

    @pytest.mark.asyncio
    async def test_system_notice_forwarded_to_active_container(self):
        """System notices SHOULD be forwarded when a container is already
        active — the agent is awake and should see the notice."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        notice = _make_message(
            "[System Notice] Auto-rebased 3 commit(s) onto your worktree.",
            sender="system_notice",
            sender_name="System",
            timestamp="new-ts",
        )

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([notice], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[notice],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # The notice queues delivery at the active turn's next tool boundary.
        deps.queue.clear_pending_tasks.assert_called_once_with(RuntimeId(group.folder))
        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(
            RuntimeId(group.folder)
        )
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_with_user_message_wakes_agent(self):
        """A system notice mixed with a real user message should wake
        the agent normally."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        notice = _make_message(
            "[System Notice] Auto-rebased 1 commit(s).",
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
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([notice, user_msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[notice, user_msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Agent SHOULD be woken up because there's a real user message.
        deps.start_interactive_turn.assert_awaited_once_with(jid)
        deps.queue.enqueue_message_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_btw_routed_normally_when_no_active_container(self):
        """'btw ...' when no container is active at all should be routed
        normally — enqueued for a fresh container run."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # No active container at all
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        msg = _make_message("btw here's some info", timestamp="new-ts")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Falls through to normal Temporal wake-up.
        deps.start_interactive_turn.assert_awaited_once_with(jid)
        deps.queue.enqueue_message_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_sunrise_reaction_on_wake(self):
        """When a message wakes a sleeping workspace, the first message
        in the batch should get a :sunrise: reaction."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        msg1 = _make_message("hello", message_id="msg-1", timestamp="ts-1")
        msg2 = _make_message("world", message_id="msg-2", timestamp="ts-2")

        with (
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg1, msg2], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg1, msg2],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Only the first message gets sunrise
        deps.send_reaction_to_channels.assert_awaited_once_with(
            jid, "msg-1", msg1.sender, "sunrise"
        )
        # Still starts the durable turn
        deps.start_interactive_turn.assert_awaited_once_with(jid)
        deps.queue.enqueue_message_check.assert_not_called()
