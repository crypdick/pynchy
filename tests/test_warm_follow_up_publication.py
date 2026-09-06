"""Warm container publication through host-owned current-turn authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.agent_protocol.api import InFlightTurn, InFlightWorkKind
from pynchy.conversation.api import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    routed_conversation_folder,
)
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.git_ops.api import RepoContext
from pynchy.identifiers import GroupFolder
from pynchy.state import begin_in_flight_turn, create_work_item_claim, resolve_conversation
from pynchy.work_items.api import WorkItemClaimRequest

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.git_policy_support import GitPolicyDeps


@dataclass(frozen=True)
class ExecutionFixture:
    workspace: str
    linear_issue_id: str
    linear_issue_identifier: str


def _turn(
    turn_id: str,
    source_group: str,
    task_id: str | None = None,
) -> InFlightTurn:
    return InFlightTurn(
        turn_id=turn_id,
        chat_jid="linear:test",
        group_folder=source_group,
        work_kind=InFlightWorkKind.SCHEDULED,
        input_messages=[],
        input_start_cursor="",
        input_end_cursor="",
        started_at="2026-08-19T21:00:00+00:00",
        task_id=task_id,
    )


async def test_warm_follow_up_uses_host_turn_and_attaches_published_pr(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
) -> None:
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace="agent-1",
            issue={
                "id": "linear-issue-1",
                "identifier": "SYN-173",
                "url": "https://linear.app/example/issue/SYN-173",
                "updatedAt": "2026-08-19T20:00:00+00:00",
                "state": {"id": "state-in-progress", "name": "In Progress"},
            },
            turn_id="turn-initial",
            task_id="task-initial",
            initiated_by="linear-webhook:test",
            request_id="claim-1",
        )
    )
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:synapse:issue"),
            key=ConversationSubjectKey(execution.linear_issue_id),
        ),
        GroupFolder(execution.workspace),
    )
    source_group = routed_conversation_folder(execution.workspace, conversation.id)
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-follow-up",
            chat_jid="linear:follow-up",
            group_folder=source_group,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-08-19T21:00:00+00:00",
            task_id="task-follow-up",
        )
    )
    assert execution.turn_id == "turn-initial"
    result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
    result_dir.mkdir(parents=True)
    repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
            return_value=[repo_ctx],
        ) as resolve_publication_repos,
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.VALID,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
            return_value={
                "success": True,
                "message": "Opened PR",
                "pr_url": "https://github.com/owner/repo/pull/104",
            },
        ) as create_pr,
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.attach_work_item_pull_request",
            new_callable=AsyncMock,
            return_value=None,
        ) as attach_pr,
    ):
        await dispatch(
            {
                "type": "sync_worktree_to_main",
                "request_id": "req-follow-up",
                "publication": "pull-request",
                "title": "Fix publication routing",
                "body": "## Summary\nFix publication routing.",
            },
            source_group,
            False,
            git_policy_deps,
        )

    resolve_publication_repos.assert_called_once_with(source_group, "turn-follow-up")
    create_pr.assert_called_once_with(
        source_group,
        repo_ctx,
        publication_branch="syn/173/fix-publication-routing",
        pr_title="Fix publication routing",
        pr_body="## Summary\nFix publication routing.\n\nResolves SYN-173",
    )
    attach_pr.assert_awaited_once_with(
        "agent-1",
        "linear-issue-1",
        "owner/repo",
        "https://github.com/owner/repo/pull/104",
    )
    result = json.loads((result_dir / "req-follow-up.json").read_text())
    assert result["success"] is True


async def test_non_routed_follow_up_resolves_execution_by_stable_task(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
) -> None:
    source_group = "agent-1"
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace=source_group,
            issue={
                "id": "linear-issue-task",
                "identifier": "SYN-174",
                "url": "https://linear.app/example/issue/SYN-174",
                "updatedAt": "2026-08-19T20:00:00+00:00",
                "state": {"id": "state-in-progress", "name": "In Progress"},
            },
            turn_id="turn-initial",
            task_id="task-stable",
            initiated_by="linear-webhook:test",
            request_id="claim-task",
        )
    )
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-follow-up",
            chat_jid="linear:follow-up",
            group_folder=source_group,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-08-19T21:00:00+00:00",
            task_id="task-stable",
        )
    )
    result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
    result_dir.mkdir(parents=True)
    repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
            return_value=[repo_ctx],
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.VALID,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
            return_value={
                "success": True,
                "message": "Opened PR",
                "pr_url": "https://github.com/owner/repo/pull/105",
            },
        ) as create_pr,
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.attach_work_item_pull_request",
            new_callable=AsyncMock,
            return_value=None,
        ) as attach_pr,
    ):
        await dispatch(
            {
                "type": "sync_worktree_to_main",
                "request_id": "req-task-follow-up",
                "publication": "pull-request",
                "title": "Fix task publication",
                "body": "Summary",
            },
            source_group,
            False,
            git_policy_deps,
        )

    assert execution.turn_id == "turn-initial"
    create_pr.assert_called_once_with(
        source_group,
        repo_ctx,
        publication_branch="syn/174/fix-task-publication",
        pr_title="Fix task publication",
        pr_body="Summary\n\nResolves SYN-174",
    )
    attach_pr.assert_awaited_once_with(
        source_group,
        execution.linear_issue_id,
        repo_ctx.slug,
        "https://github.com/owner/repo/pull/105",
    )
    result = json.loads((result_dir / "req-task-follow-up.json").read_text())
    assert result["success"] is True


@pytest.mark.parametrize("task_id", [None, "task-without-execution"])
async def test_non_routed_publication_without_owned_execution(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
    task_id: str | None,
) -> None:
    source_group = "agent-1"
    await begin_in_flight_turn(_turn("turn-without-execution", source_group, task_id))
    result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
    result_dir.mkdir(parents=True)
    repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
            return_value=[repo_ctx],
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.VALID,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
            return_value={
                "success": True,
                "message": "Opened PR",
                "pr_url": "https://github.com/owner/repo/pull/106",
            },
        ) as create_pr,
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.attach_work_item_pull_request",
            new_callable=AsyncMock,
        ) as attach_pr,
    ):
        await dispatch(
            {
                "type": "sync_worktree_to_main",
                "request_id": "req-without-execution",
                "publication": "pull-request",
                "title": "Publish unrelated work",
                "body": "Summary",
            },
            source_group,
            False,
            git_policy_deps,
        )

    result = json.loads((result_dir / "req-without-execution.json").read_text())
    assert result["success"] is True
    create_pr.assert_called_once_with(
        source_group,
        repo_ctx,
        publication_branch=None,
        pr_title="Publish unrelated work",
        pr_body="Summary",
    )
    attach_pr.assert_not_awaited()


async def test_warm_follow_up_rejects_stale_agent_turn(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
) -> None:
    source_group = "agent-1__thread_conversation-conv_1"
    result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
    result_dir.mkdir(parents=True)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_current_turn",
            new_callable=AsyncMock,
            return_value=_turn("turn-follow-up", source_group),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
        ) as create_pr,
    ):
        await dispatch(
            {
                "type": "sync_worktree_to_main",
                "request_id": "req-stale-turn",
                "publication": "pull-request",
                "turn_id": "turn-initial",
                "title": "Fix publication routing",
                "body": "Summary",
            },
            source_group,
            False,
            git_policy_deps,
        )

    create_pr.assert_not_called()
    result = json.loads((result_dir / "req-stale-turn.json").read_text())
    assert result == {
        "success": False,
        "message": "Publication blocked: request does not match the current agent turn.",
    }


@pytest.mark.parametrize(
    ("repo_result", "attachment_error", "expected"),
    [
        (
            {"success": True, "message": "Opened PR"},
            None,
            "host returned no valid canonical PR URL",
        ),
        (
            {
                "success": True,
                "message": "Opened PR",
                "pr_url": "https://github.com/other/repo/pull/104",
            },
            None,
            "host returned no valid canonical PR URL",
        ),
        (
            {
                "success": True,
                "message": "Opened PR",
                "pr_url": "https://github.com/owner/repo/pull/104",
            },
            "Linear did not create the attachment",
            "Linear attachment failed",
        ),
        (
            {"success": False, "message": "Push failed"},
            None,
            "Push failed",
        ),
    ],
)
async def test_publication_reports_missing_url_or_attachment_failure(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
    repo_result: dict[str, object],
    attachment_error: str | None,
    expected: str,
) -> None:
    source_group = "agent-1"
    result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
    result_dir.mkdir(parents=True)
    repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_current_turn",
            new_callable=AsyncMock,
            return_value=_turn("turn-follow-up", source_group),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
            new_callable=AsyncMock,
            return_value=ExecutionFixture("agent-1", "linear-issue-1", "SYN-173"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
            return_value=[repo_ctx],
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.VALID,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
            return_value=repo_result,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.attach_work_item_pull_request",
            new_callable=AsyncMock,
            return_value=attachment_error,
        ) as attach_pr,
    ):
        await dispatch(
            {
                "type": "sync_worktree_to_main",
                "request_id": "req-publication-failure",
                "publication": "pull-request",
                "title": "Fix publication routing",
                "body": "Summary",
            },
            source_group,
            False,
            git_policy_deps,
        )

    result = json.loads((result_dir / "req-publication-failure.json").read_text())
    assert result["success"] is False
    assert expected in result["repos"]["owner/repo"]["message"]
    if expected in {"host returned no valid canonical PR URL", "Push failed"}:
        attach_pr.assert_not_awaited()
