"""Bounded SQLite context exposed to security decisions."""

from __future__ import annotations

import json
from typing import Literal
from unittest.mock import patch

import pytest

from pynchy import state
from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.work_items.api import (
    WorkItemClaimRequest,
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
)


@pytest.fixture(autouse=True)
async def _setup_db() -> None:
    await state.init_test_database()


@pytest.mark.asyncio
async def test_recent_security_context_is_bounded_and_omits_tool_inputs() -> None:
    chat_jid = "security-context@test"
    for index in range(6):
        await state.store_message_direct(
            message_id=f"message-{index}",
            chat_jid=chat_jid,
            sender="user" if index % 2 == 0 else "assistant",
            sender_name="User" if index % 2 == 0 else "Assistant",
            content=f"message-{index}-" + ("x" * 600),
            timestamp=f"2026-07-19T00:00:0{index}+00:00",
            is_from_me=index % 2 == 1,
            message_type="user" if index % 2 == 0 else "assistant",
        )
    await state.store_event(
        "agent_trace",
        chat_jid,
        {"trace_type": "thinking", "tool_name": "not-an-action"},
    )
    for index in range(3):
        await state.store_event(
            "agent_trace",
            chat_jid,
            {
                "trace_type": "text",
                "content": f"update-{index}-" + ("y" * 600),
            },
        )
    for index in range(10):
        await state.store_event(
            "agent_trace",
            chat_jid,
            {
                "trace_type": "tool_use",
                "tool_name": f"Tool{index}",
                "tool_input": {
                    "secret": "must-not-cross-context-boundary",  # pragma: allowlist secret
                },
            },
        )

    context = await state.load_recent_security_context(chat_jid)

    assert context.current_user_intent is not None
    assert context.current_user_intent.startswith("message-4-")
    assert len(context.current_user_intent) == 500
    assert len(context.recent_messages) == 4
    assert all(len(message.content) == 500 for message in context.recent_messages)
    assert len(context.recent_agent_updates) == 2
    assert context.recent_agent_updates[0].startswith("update-1-")
    assert all(len(update) == 500 for update in context.recent_agent_updates)
    assert context.completed_tool_actions == tuple(f"Tool{index}" for index in range(2, 10))
    assert context.execution_authority is None
    assert "must-not-cross-context-boundary" not in repr(context)


async def _seed_linear_execution(
    *,
    task_status: Literal["active", "paused", "completed", "cancelled"] = "active",
    control_state: CheckpointControlState = CheckpointControlState.ACTIVE,
    resolve_lease: bool = True,
) -> None:
    chat_jid = "discord:thread:syn-88"
    task_id = "linear-execute-syn-88"
    await state.create_task(
        ScheduledTask(
            id=task_id,
            group_folder="pynchy",
            chat_jid="discord:project",
            prompt="Deliver SYN-88.",
            schedule_type="once",
            schedule_value="2026-07-26T00:00:00+00:00",
            session_policy=SessionPolicy.CONTINUE,
            status=task_status,
            created_at="2026-07-26T00:00:00+00:00",
            bound_chat_jid=chat_jid,
            bound_group_folder="pynchy-thread",
        )
    )
    issue = {
        "id": "issue-syn-88",
        "identifier": "SYN-88",
        "url": "https://linear.app/example/issue/SYN-88",
        "updatedAt": "2026-07-26T00:00:00+00:00",
        "state": {"id": "human-approved", "name": "Human Approved"},
    }
    execution = await state.create_work_item_claim(
        WorkItemClaimRequest(
            workspace="pynchy",
            issue=issue,
            turn_id=None,
            task_id=task_id,
            initiated_by="linear-work-item-controller",
            request_id="syn-88-lease",
        )
    )
    if resolve_lease:
        transition = await state.get_work_item_transition_by_request("syn-88-lease")
        assert transition is not None
        execution = await state.resolve_work_item_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
            issue={
                **issue,
                "state": {"id": "in-progress", "name": "In Progress"},
            },
        )
        assert execution.status is WorkItemExecutionStatus.IN_PROGRESS
    await state.begin_in_flight_turn(
        InFlightTurn(
            turn_id="scheduled-syn-88",
            chat_jid=chat_jid,
            group_folder="pynchy-thread",
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[{"content": "Deliver SYN-88."}],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-26T00:00:01+00:00",
            task_id=task_id,
            input_source="trusted:linear:authorized",
            control_state=control_state,
        )
    )


@pytest.mark.asyncio
async def test_active_linear_lease_is_exposed_as_durable_execution_authority() -> None:
    await _seed_linear_execution()
    await state.store_message_direct(
        message_id="keep-working",
        chat_jid="discord:thread:syn-88",
        sender="user",
        sender_name="User",
        content="I'm going to sleep. Keep working on this.",
        timestamp="2026-07-26T00:00:02+00:00",
        is_from_me=False,
        message_type="user",
    )

    context = await state.load_recent_security_context("discord:thread:syn-88")

    assert context.current_user_intent == "I'm going to sleep. Keep working on this."
    assert context.execution_authority is not None
    assert context.execution_authority.kind.value == "linear_work_item_lease"
    assert context.execution_authority.work_item_identifier == "SYN-88"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "control_state", "resolve_lease"),
    [
        ("paused", CheckpointControlState.ACTIVE, True),
        ("cancelled", CheckpointControlState.ACTIVE, True),
        ("active", CheckpointControlState.PAUSE_REQUESTED, True),
        ("active", CheckpointControlState.PAUSED, True),
        ("active", CheckpointControlState.RESET_REQUESTED, True),
        ("active", CheckpointControlState.ACTIVE, False),
    ],
)
async def test_frozen_or_unleased_work_has_no_durable_execution_authority(
    task_status: Literal["active", "paused", "completed", "cancelled"],
    control_state: CheckpointControlState,
    resolve_lease: bool,
) -> None:
    await _seed_linear_execution(
        task_status=task_status,
        control_state=control_state,
        resolve_lease=resolve_lease,
    )

    context = await state.load_recent_security_context("discord:thread:syn-88")

    assert context.execution_authority is None


@pytest.mark.asyncio
async def test_malformed_agent_update_payload_is_ignored() -> None:
    chat_jid = "security-context-malformed-update"
    await state.store_event(
        "agent_trace",
        chat_jid,
        {"trace_type": "text", "content": "update"},
    )

    with patch(
        "pynchy.state.security_context.json.loads",
        side_effect=json.JSONDecodeError("bad payload", "", 0),
    ):
        context = await state.load_recent_security_context(chat_jid)

    assert context.recent_agent_updates == ()

    with patch(
        "pynchy.state.security_context.json.loads",
        return_value={"trace_type": "text", "content": ""},
    ):
        empty = await state.load_recent_security_context(chat_jid)
    assert empty.recent_agent_updates == ()


@pytest.mark.asyncio
async def test_malformed_tool_payload_and_trace_type_drift_are_ignored() -> None:
    chat_jid = "security-context-malformed-tool"
    await state.store_event(
        "agent_trace",
        chat_jid,
        {"trace_type": "tool_use", "tool_name": "Tool"},
    )

    with patch(
        "pynchy.state.security_context.json.loads",
        side_effect=TypeError("bad payload"),
    ):
        malformed = await state.load_recent_security_context(chat_jid)
    assert malformed.completed_tool_actions == ()

    with patch(
        "pynchy.state.security_context.json.loads",
        return_value={"trace_type": "text", "content": "not a tool"},
    ):
        drifted = await state.load_recent_security_context(chat_jid)
    assert drifted.completed_tool_actions == ()

    with patch(
        "pynchy.state.security_context.json.loads",
        return_value={"trace_type": "tool_use", "tool_name": ""},
    ):
        unnamed = await state.load_recent_security_context(chat_jid)
    assert unnamed.completed_tool_actions == ()
