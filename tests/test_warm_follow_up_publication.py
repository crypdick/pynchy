"""Warm container publication through host-owned current-turn authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.git_ops.api import RepoContext

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.git_policy_support import GitPolicyDeps


@dataclass(frozen=True)
class Turn:
    turn_id: str


@dataclass(frozen=True)
class Execution:
    workspace: str
    linear_issue_id: str
    linear_issue_identifier: str


async def test_warm_follow_up_uses_host_turn_and_attaches_published_pr(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
) -> None:
    source_group = "agent-1__thread_conversation-conv_1"
    result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
    result_dir.mkdir(parents=True)
    repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")
    execution = Execution("agent-1", "linear-issue-1", "SYN-173")

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_current_turn",
            new_callable=AsyncMock,
            return_value=Turn("turn-follow-up"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
            new_callable=AsyncMock,
            return_value=execution,
        ) as get_execution,
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

    get_execution.assert_awaited_once_with("turn-follow-up")
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
            return_value=Turn("turn-follow-up"),
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
    ],
)
async def test_publication_reports_missing_url_or_attachment_failure(
    git_policy_deps: GitPolicyDeps,
    tmp_path: Path,
    repo_result: dict[str, object],
    attachment_error: str | None,
    expected: str,
) -> None:
    source_group = "agent-1__thread_conversation-conv_1"
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
            return_value=Turn("turn-follow-up"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
            new_callable=AsyncMock,
            return_value=Execution("agent-1", "linear-issue-1", "SYN-173"),
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
    if expected == "host returned no valid canonical PR URL":
        attach_pr.assert_not_awaited()
