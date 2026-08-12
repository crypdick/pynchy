"""Public PR metadata validation tests."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.container_manager.ipc.handlers_lifecycle import publication_metadata


@dataclass(frozen=True)
class LinearExecutionFixture:
    linear_issue_identifier: str


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {"title": "", "body": "body"},
            "Publication blocked: PR title must be non-empty and at most 256 bytes.",
        ),
        (
            {"title": "Title", "body": ""},
            "Publication blocked: PR body must be non-empty and at most 64 KiB.",
        ),
    ],
)
async def test_publication_metadata_rejects_empty_pr_fields(
    data: dict[str, str], message: str
) -> None:
    assert await publication_metadata(data, None) == message


async def test_linear_publication_metadata_derives_branch_name() -> None:
    with patch(
        "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
        new=AsyncMock(return_value=LinearExecutionFixture(linear_issue_identifier="SYN-247")),
    ):
        metadata = await publication_metadata(
            {"title": "Fix login", "body": "## Summary\nFix the login flow."},
            "turn-syn-247",
        )

    assert metadata == (
        "Fix login",
        "## Summary\nFix the login flow.\n\nResolves SYN-247",
        "syn/247/fix-login",
    )


async def test_linear_publication_metadata_keeps_existing_resolve_link() -> None:
    with patch(
        "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
        new=AsyncMock(return_value=LinearExecutionFixture(linear_issue_identifier="SYN-247")),
    ):
        metadata = await publication_metadata(
            {"title": "Fix login", "body": "## Summary\n\nresolves syn-247"},
            "turn-syn-247",
        )

    assert metadata == (
        "Fix login",
        "## Summary\n\nresolves syn-247",
        "syn/247/fix-login",
    )


async def test_linear_publication_metadata_rejects_oversized_resolve_link() -> None:
    footer = "\n\nResolves SYN-247"
    body = "x" * (64 * 1024 - len(footer) + 1)
    with patch(
        "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
        new=AsyncMock(return_value=LinearExecutionFixture(linear_issue_identifier="SYN-247")),
    ):
        metadata = await publication_metadata({"title": "Fix login", "body": body}, "turn")

    assert metadata == "Publication blocked: PR body with Linear resolve link exceeds 64 KiB."


async def test_publication_metadata_keeps_generic_worktree_branch_unchanged() -> None:
    assert await publication_metadata({"title": "Fix login", "body": "body"}, None) == (
        "Fix login",
        "body",
        None,
    )


@pytest.mark.parametrize(
    ("execution", "title", "expected"),
    [
        (None, "Fix login", ("Fix login", "body", None)),
        (
            LinearExecutionFixture(linear_issue_identifier="BAD_IDENTIFIER"),
            "Fix login",
            "Publication blocked: Linear issue identifier cannot form a branch name.",
        ),
        (
            LinearExecutionFixture(linear_issue_identifier="SYN-247"),
            "!!!",
            "Publication blocked: PR title cannot form a branch name.",
        ),
    ],
)
async def test_linear_publication_metadata_rejects_unusable_branch_inputs(
    execution: LinearExecutionFixture | None,
    title: str,
    expected: tuple[str, str, None] | str,
) -> None:
    with patch(
        "pynchy.host.container_manager.ipc.handlers_lifecycle.get_work_item_execution_for_turn",
        new=AsyncMock(return_value=execution),
    ):
        metadata = await publication_metadata({"title": title, "body": "body"}, "turn")

    assert metadata == expected
