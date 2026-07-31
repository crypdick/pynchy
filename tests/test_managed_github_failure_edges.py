"""Public managed-feature publication behavior for GitHub lookup failures."""

from __future__ import annotations

import subprocess  # noqa: S404 - test doubles model the fixed gh subprocess.
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import host_create_pr_from_managed_feature
from tests.git_policy_support import (
    create_managed_feature,
    git,
    managed_pr_result,
    managed_record,
    no_pr_result,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)


@pytest.mark.action("lifecycle.managed.feature.publish")
@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (
            "",
            "Publication blocked: existing managed feature PR lookup returned no data.",
        ),
        (
            "not json",
            "Publication blocked: existing managed feature PR lookup returned invalid data.",
        ),
        (
            '{"url": "https://github.com/owner/repo/pull/1"}',
            "Publication blocked: existing managed feature PR lookup returned invalid data.",
        ),
        (
            "[{}, {}]",
            "Publication blocked: multiple managed feature PRs use this branch.",
        ),
    ],
)
def test_rejects_unusable_existing_pr_lookup(git_env: dict, stdout: str, message: str) -> None:
    """Malformed or ambiguous GitHub lookup data cannot authorize publication."""
    feature = "invalid-pr-lookup"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    real_run = subprocess.run

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return real_run(args, check=kwargs.pop("check", False), **kwargs)

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {"success": False, "message": message}
    assert feature not in git(git_env["origin"], "branch").stdout


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rejects_github_lookup_process_failure(git_env: dict) -> None:
    """A failed GitHub process never becomes permission to push the branch."""
    feature = "process-failure-pr-lookup"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    real_run = subprocess.run

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            raise OSError("gh unavailable")
        return real_run(args, check=kwargs.pop("check", False), **kwargs)

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: could not inspect existing managed feature PR.",
    }
    assert feature not in git(git_env["origin"], "branch").stdout


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_does_not_close_untrusted_created_pr_url(git_env: dict) -> None:
    """A failed verification never closes a PR outside the configured repository."""
    feature = "untrusted-created-pr"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
    gh_calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            gh_calls.append(args)
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="https://github.com/other/repo/pull/1\n",
                    stderr="",
                )
            return no_pr_result(args)
        return real_run(args, check=kwargs.pop("check", False), **kwargs)

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": (
            "Publication blocked: could not identify newly created managed feature PR."
            " The host could not close it."
        ),
    }
    assert head_sha != base_sha
    assert [call[1:3] for call in gh_calls].count(["pr", "close"]) == 0


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_closes_a_configured_created_pr_when_verification_is_missing(git_env: dict) -> None:
    """A created PR is compensated when the follow-up lookup cannot verify it."""
    feature = "unverifiable-created-pr"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    gh_calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            gh_calls.append(args)
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/1\n",
                    stderr="",
                )
            if args[1:3] == ["pr", "close"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return no_pr_result(args)
        return real_run(args, check=kwargs.pop("check", False), **kwargs)

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": (
            "Publication blocked: could not verify managed feature PR after creation. "
            "The host closed the newly created PR."
        ),
    }
    assert [call[1:3] for call in gh_calls].count(["pr", "close"]) == 1
    assert worktree.exists()


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_reports_created_pr_close_failure(git_env: dict) -> None:
    """A verification failure reports when compensating PR closure also fails."""
    feature = "close-failure-pr"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
    list_calls = 0
    real_run = subprocess.run

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal list_calls
        if args[0] == "gh":
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/1\n",
                    stderr="",
                )
            if args[1:3] == ["pr", "close"]:
                raise OSError("close unavailable")
            list_calls += 1
            if list_calls < 3:
                return no_pr_result(args)
            return managed_pr_result(args, base_sha=base_sha, head_sha=base_sha)
        return real_run(args, check=kwargs.pop("check", False), **kwargs)

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": (
            "Publication blocked: existing PR is not open or does not match managed feature "
            "inspection. The host could not close it."
        ),
    }
    assert head_sha != base_sha
