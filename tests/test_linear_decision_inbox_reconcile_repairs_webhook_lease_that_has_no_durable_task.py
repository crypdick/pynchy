"""Behavioral tests for host-leased Linear execution admission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.models import ConversationSubjectKey
from pynchy.identifiers import GroupFolder
from pynchy.plugins.integrations.linear_decision_inbox import (
    reconcile_linear_decision_inbox,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_work_item_lease,
)
from pynchy.scheduling.api import (
    TaskRunLog,
)
from pynchy.state import (
    begin_in_flight_turn,
    get_active_work_item_execution,
    get_all_tasks,
    get_conversation_for_subject_key,
    get_in_flight_turn_for_task,
    get_task_by_id,
    log_task_run,
    record_task_completion,
    update_task,
)
from tests.linear_decision_inbox_support import (
    _board,
    _DecisionClient,
    _issue,
    _LinearAccount,
    _Workspace,
)

pytest_plugins = ("tests.linear_decision_inbox_support",)

if TYPE_CHECKING:
    import pytest


async def test_reconcile_repairs_webhook_lease_that_has_no_durable_task() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = [
        _issue(
            "issue-webhook",
            "SYN-13",
            "Resume webhook work",
            "human_approved",
            "project-beta",
        )
    ]
    board = _board("project-beta")
    execution = await acquire_work_item_lease(
        client,
        WorkItemLeaseRequest(
            workspace="beta",
            issue_id="issue-webhook",
            request_id="linear-webhook:delivery-1:lease",
            initiated_by="linear-webhook:delivery-1",
            board=board,
        ),
    )
    assert execution.task_id is None

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": board},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert len(created) == 1
    assert created[0].id.startswith("linear-execute-syn-13-")
    repaired = await get_active_work_item_execution("issue-webhook")
    assert repaired is not None
    assert repaired.task_id == created[0].id
    assert repaired.temporal_workflow_id is not None


async def test_reconcile_does_not_infer_authority_from_unleased_in_progress() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = []
    client.issues_by_state["state-progress"] = [
        _issue(
            "issue-unleased",
            "SYN-99",
            "Do not infer approval",
            "in_progress",
            "project-beta",
        )
    ]

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert created == []
    assert await get_active_work_item_execution("issue-unleased") is None


async def test_reconcile_reactivates_quiet_completed_task_after_grace_period() -> None:
    client = _DecisionClient()
    workspace = _Workspace("beta", "Beta", "linear:beta")
    board = _board("project-beta")
    observed_at = datetime.now(UTC)
    created = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at,
    )
    task = created[0]
    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=observed_at.isoformat(),
            duration_ms=1,
            status="success",
        )
    )
    await record_task_completion(task.id, last_result="Stopped without transition", completed=True)

    recovered = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at + timedelta(minutes=6),
    )

    assert [item.id for item in recovered] == [task.id]
    active = await get_task_by_id(task.id)
    assert active is not None
    assert active.status == "active"
    assert active.schedule_value == (observed_at + timedelta(minutes=6)).isoformat()


async def test_reconcile_reactivates_moved_active_execution_in_original_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_account_for_workspace",
        lambda _workspace: _LinearAccount(name="linear"),
    )
    original = _Workspace("beta", "Beta", "linear:beta")
    destination = _Workspace("alpha", "Alpha", "linear:alpha")
    boards = {
        original.folder: _board("project-beta"),
        destination.folder: _board("project-alpha"),
    }
    observed_at = datetime.now(UTC)
    task = (
        await reconcile_linear_decision_inbox(
            client,
            [destination, original],
            boards,
            now=observed_at,
        )
    )[0]
    assert task.conversation_id is not None
    await record_task_completion(task.id, last_result="Stopped without transition", completed=True)

    issue = client.issues_by_state["state-progress"][0]
    issue["project"] = {"id": "project-alpha", "name": "project-alpha"}

    recovered = await reconcile_linear_decision_inbox(
        client,
        [destination, original],
        boards,
        now=observed_at + timedelta(minutes=6),
    )

    assert [item.id for item in recovered] == [task.id]
    assert recovered[0].group_folder == original.folder
    assert recovered[0].chat_jid == original.jid
    assert recovered[0].conversation_id == task.conversation_id
    active_task = await get_task_by_id(task.id)
    assert active_task is not None
    assert active_task.status == "active"
    assert active_task.group_folder == original.folder
    assert active_task.chat_jid == original.jid
    assert active_task.conversation_id == task.conversation_id
    assert len(await get_all_tasks()) == 1
    assert (
        await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-execute"),
            workspace=GroupFolder(destination.folder),
            namespace_suffix=":issue",
        )
        is None
    )
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    assert execution.workspace == original.folder
    assert execution.task_id == task.id


async def test_reconcile_resumes_paused_execution_after_grace_and_clears_terminal_turn() -> None:
    client = _DecisionClient()
    workspace = _Workspace("beta", "Beta", "linear:beta")
    board = _board("project-beta")
    observed_at = datetime.now(UTC)
    task = (
        await reconcile_linear_decision_inbox(
            client,
            [workspace],
            {"beta": board},
            now=observed_at,
        )
    )[0]
    await record_task_completion(task.id, last_result="Repeated terminal failure", completed=False)
    await update_task(task.id, {"status": "paused"})
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="terminal-scheduled-turn",
            chat_jid=task.chat_jid,
            group_folder=task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at=observed_at.isoformat(),
            task_id=task.id,
        )
    )

    assert (
        await reconcile_linear_decision_inbox(
            client,
            [workspace],
            {"beta": board},
            now=observed_at + timedelta(minutes=1),
        )
        == []
    )
    paused = await get_task_by_id(task.id)
    assert paused is not None
    assert paused.status == "paused"
    assert paused.occurrence_generation == 0
    assert await get_in_flight_turn_for_task(task.id) is not None

    resumed = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at + timedelta(minutes=6),
    )

    assert [item.id for item in resumed] == [task.id]
    active = await get_task_by_id(task.id)
    assert active is not None
    assert active.status == "active"
    assert active.occurrence_generation == 1
    assert active.superseded_occurrence_generation == 0
    assert await get_in_flight_turn_for_task(task.id) is None
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    assert execution.temporal_workflow_id is not None
    assert execution.temporal_workflow_id.endswith("-resume-1")


async def test_reconcile_bounds_incomplete_outcome_recovery() -> None:
    client = _DecisionClient()
    workspace = _Workspace("beta", "Beta", "linear:beta")
    board = _board("project-beta")
    observed_at = datetime.now(UTC)
    created = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at,
    )
    task = created[0]
    for attempt in range(3):
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=(observed_at + timedelta(seconds=attempt)).isoformat(),
                duration_ms=1,
                status="incomplete",
            )
        )
    await record_task_completion(
        task.id,
        last_result="Incomplete: no Linear lifecycle outcome",
        completed=True,
    )

    recovered = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at + timedelta(minutes=6),
    )

    assert recovered == []
    bounded = await get_task_by_id(task.id)
    assert bounded is not None
    assert bounded.status == "completed"
