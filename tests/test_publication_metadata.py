"""Public PR metadata validation tests."""

from __future__ import annotations

from dataclasses import dataclass

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
def test_publication_metadata_rejects_empty_pr_fields(data: dict[str, str], message: str) -> None:
    assert publication_metadata(data, None) == message


def test_linear_publication_metadata_derives_branch_name() -> None:
    metadata = publication_metadata(
        {"title": "Fix login", "body": "## Summary\nFix the login flow."},
        LinearExecutionFixture(linear_issue_identifier="SYN-247"),
    )

    assert metadata == (
        "Fix login",
        "## Summary\nFix the login flow.\n\nResolves SYN-247",
        "syn/247/fix-login",
    )


def test_linear_publication_metadata_keeps_existing_resolve_link() -> None:
    metadata = publication_metadata(
        {"title": "Fix login", "body": "## Summary\n\nresolves syn-247"},
        LinearExecutionFixture(linear_issue_identifier="SYN-247"),
    )

    assert metadata == (
        "Fix login",
        "## Summary\n\nresolves syn-247",
        "syn/247/fix-login",
    )


def test_linear_publication_metadata_rejects_oversized_resolve_link() -> None:
    footer = "\n\nResolves SYN-247"
    body = "x" * (64 * 1024 - len(footer) + 1)
    metadata = publication_metadata(
        {"title": "Fix login", "body": body},
        LinearExecutionFixture(linear_issue_identifier="SYN-247"),
    )

    assert metadata == "Publication blocked: PR body with Linear resolve link exceeds 64 KiB."


def test_publication_metadata_keeps_generic_worktree_branch_unchanged() -> None:
    assert publication_metadata({"title": "Fix login", "body": "body"}, None) == (
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
def test_linear_publication_metadata_rejects_unusable_branch_inputs(
    execution: LinearExecutionFixture | None,
    title: str,
    expected: tuple[str, str, None] | str,
) -> None:
    metadata = publication_metadata({"title": title, "body": "body"}, execution)

    assert metadata == expected
