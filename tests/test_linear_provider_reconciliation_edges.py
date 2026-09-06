"""Public recovery edges for missed Linear provider callbacks."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.plugins.integrations.linear_provider_reconciliation import (
    LinearDecisionInboxRuntime,
    configure_linear_decision_inbox_runtime,
    reconcile_provider_work_item_state,
    retire_globally_unavailable_work_item,
)
from pynchy.work_items.api import WorkItemExecutionStatus, WorkItemTransitionStatus
from tests.test_linear_provider_state_reconciliation import (
    _board,
    _Client,
    _configure_runtime,
    _execution,
    _transition,
)


async def test_reconciliation_requires_configured_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_provider_reconciliation._runtime", None)

    with pytest.raises(RuntimeError, match="runtime has not been configured"):
        await reconcile_provider_work_item_state(_Client(None), {})


async def test_globally_unavailable_terminal_work_is_retired() -> None:
    execution = replace(_execution(), status=WorkItemExecutionStatus.COMPLETED)
    retire_unowned = AsyncMock(return_value=False)
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=AsyncMock(),
            retire_execution=AsyncMock(),
            retire_terminal_execution_if_unowned=retire_unowned,
            retire_terminal_execution=AsyncMock(),
        )
    )

    assert await retire_globally_unavailable_work_item(execution) is True
    retire_unowned.assert_awaited_once_with(execution)


async def test_globally_unavailable_active_work_is_cancelled_and_retired() -> None:
    execution = _execution()
    cancelled = replace(execution, status=WorkItemExecutionStatus.CANCELLED)
    cancel = AsyncMock(return_value=cancelled)
    retire = AsyncMock()
    _configure_runtime(execution, cancel=cancel, retire=retire)

    assert await retire_globally_unavailable_work_item(execution) is True
    cancel.assert_awaited_once_with(
        execution.id,
        blocker="Linear state no longer authorizes this execution: issue is unavailable",
    )
    retire.assert_awaited_once_with(cancelled)


async def test_unknown_execution_without_transition_is_left_for_later_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = replace(_execution(), status=WorkItemExecutionStatus.UNKNOWN)
    _configure_runtime(execution)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.linear_account_for_workspace",
        lambda _workspace: None,
    )

    issue = {
        "id": "issue-1",
        "state": _state("In Progress"),
        "project": {"id": "project-1"},
    }
    assert await reconcile_provider_work_item_state(_Client(issue), {"pynchy": _board()}) == 0


async def test_stale_transition_with_terminal_resolution_is_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    transition = _transition(status=WorkItemTransitionStatus.UNKNOWN)
    resolved = replace(execution, status=WorkItemExecutionStatus.COMPLETED)
    retire = AsyncMock()
    _configure_runtime(execution, transition=transition, retire=retire)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        AsyncMock(return_value=resolved),
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Done"),
        "project": {"id": "project-1"},
    }

    assert await reconcile_provider_work_item_state(_Client(issue), {"pynchy": _board()}) == 1
    retire.assert_awaited_once_with(resolved)


async def test_unresolved_provider_transition_can_remain_unsettled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    transition = _transition(status=WorkItemTransitionStatus.UNKNOWN)
    _configure_runtime(execution, transition=transition)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        AsyncMock(return_value=None),
    )
    issue = {
        "id": "issue-1",
        "state": _state("In Progress"),
        "project": {"id": "project-1"},
    }

    assert await reconcile_provider_work_item_state(_Client(issue), {"pynchy": _board()}) == 0


async def test_invalid_pending_transition_timestamp_is_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    transition = _transition(status=WorkItemTransitionStatus.PENDING, created_at="invalid")
    _configure_runtime(execution, transition=transition)
    reconcile = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        reconcile,
    )
    issue = {
        "id": "issue-1",
        "state": _state("In Progress"),
        "project": {"id": "project-1"},
    }

    assert await reconcile_provider_work_item_state(_Client(issue), {"pynchy": _board()}) == 0
    reconcile.assert_awaited_once()


async def test_naive_recent_pending_transition_stays_with_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    transition = _transition(
        status=WorkItemTransitionStatus.PENDING,
        created_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
    )
    _configure_runtime(execution, transition=transition)
    reconcile = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        reconcile,
    )
    issue = {
        "id": "issue-1",
        "state": _state("In Progress"),
        "project": {"id": "project-1"},
    }

    assert await reconcile_provider_work_item_state(_Client(issue), {"pynchy": _board()}) == 0
    reconcile.assert_not_awaited()


async def test_done_reconciliation_keeps_work_when_completion_evidence_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    retire_terminal = AsyncMock()
    _configure_runtime(execution, retire_terminal=retire_terminal)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.complete_reviewed_work_item",
        AsyncMock(return_value=None),
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Done"),
        "project": {"id": "project-1"},
    }

    assert await reconcile_provider_work_item_state(_Client(issue), {"pynchy": _board()}) == 0
    retire_terminal.assert_not_awaited()


async def test_provider_failure_isolated_to_one_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    execution = _execution()
    _configure_runtime(execution)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.linear_account_for_workspace",
        lambda _workspace: None,
    )
    client = _Client({"id": "issue-1", "project": {"id": "project-1"}})
    client.get_issue = AsyncMock(side_effect=RuntimeError("provider unavailable"))  # type: ignore[method-assign]

    assert await reconcile_provider_work_item_state(client, {"pynchy": _board()}) == 0


async def test_terminal_repair_candidate_without_latest_execution_is_ignored() -> None:
    execution = _execution()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[]),
            list_terminal_repair_candidates=AsyncMock(return_value=[execution]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=AsyncMock(),
            retire_execution=AsyncMock(),
            retire_terminal_execution_if_unowned=AsyncMock(),
            retire_terminal_execution=AsyncMock(),
        )
    )

    assert await reconcile_provider_work_item_state(_Client(None), {}) == 0


async def test_reconciliation_skips_execution_owned_by_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    _configure_runtime(execution)
    account = Mock()
    account.name = "other-account"
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.linear_account_for_workspace",
        lambda _workspace: account,
    )
    client = _Client({"id": "issue-1"})

    assert (
        await reconcile_provider_work_item_state(
            client,
            {"pynchy": _board()},
            account_name="requested-account",
        )
        == 0
    )


async def test_unavailable_probe_records_the_account_without_retiring_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    _configure_runtime(execution)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.linear_account_for_workspace",
        lambda _workspace: None,
    )
    probes: dict[str, object] = {}

    assert (
        await reconcile_provider_work_item_state(
            _Client(None),
            {"pynchy": _board()},
            account_name="requested-account",
            unavailable_probes=probes,
        )
        == 0
    )
    probe = probes[execution.id]
    assert probe.account_names == {"requested-account"}
    assert (
        await reconcile_provider_work_item_state(
            _Client(None),
            {"pynchy": _board()},
            account_name="requested-account",
        )
        == 0
    )


def _state(name: str) -> dict[str, str]:
    slug = name.casefold().replace(" ", "-")
    return {"id": f"state-{slug}", "name": name}
