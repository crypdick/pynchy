"""Regression tests for managed alternate-object transport revalidation."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - test double models fixed gh subprocess responses.
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import (
    host_create_pr_from_managed_feature,
    resolve_managed_feature_publication,
    run_git,
)
from tests.git_policy_support import (
    create_managed_feature,
    git,
    managed_pr_result,
    managed_record,
    no_pr_result,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rechecks_object_store_between_isolated_git_invocations(
    git_env: dict,
    tmp_path: Path,
) -> None:
    """A mutation after isolated Git initialization blocks the next command."""
    project = git_env["project"]
    create_managed_feature(git_env, "transport-recheck")
    write_managed_manifest(project, [managed_record("transport-recheck")])
    pack_dir = project / ".git" / "objects" / "pack"
    pack_dir.mkdir(exist_ok=True)

    def mutate_after_init(*args, **kwargs):
        result = run_git(*args, **kwargs)
        if args[:2] == ("init", "--bare") and kwargs["env"].get("GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            moved_pack = tmp_path / "moved-pack"
            pack_dir.rename(moved_pack)
            pack_dir.symlink_to(moved_pack, target_is_directory=True)
        return result

    with patch(
        "pynchy.host.git_ops.managed_feature.run_git",
        side_effect=mutate_after_init,
    ):
        resolution = resolve_managed_feature_publication("transport-recheck", [git_env["repo_ctx"]])

    assert resolution.publication is None
    assert resolution.error == (
        "Publication blocked: configured repository 'owner/repo' object store is unavailable."
    )


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rejects_manifest_binding_changed_after_inspection(git_env: dict) -> None:
    """A same-commit branch rewrite cannot switch approved PR target."""
    worktree = create_managed_feature(git_env, "bound-feature")
    write_managed_manifest(git_env["project"], [managed_record("bound-feature")])
    inspected_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    git(worktree, "checkout", "-b", "renamed-feature")
    write_managed_manifest(
        git_env["project"],
        [managed_record("bound-feature", branch="renamed-feature")],
    )

    with (
        patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None),
        patch(
            "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
            return_value=str(git_env["origin"]),
        ),
        patch("pynchy.host.git_ops.sync.subprocess.run") as gh,
    ):
        result = host_create_pr_from_managed_feature(
            "bound-feature",
            [git_env["repo_ctx"]],
            expected_binding={
                "feature_slug": "bound-feature",
                "repository": "owner/repo",
                "branch": "bound-feature",
                "target_branch": "main",
                "base_sha": base_sha,
                "head_sha": inspected_head,
            },
        )

    assert result["success"] is False
    assert "changed after Cop inspection" in result["message"]
    gh.assert_not_called()
    assert "renamed-feature" not in git(git_env["origin"], "branch").stdout


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_blocks_object_store_mutation_before_push(git_env: dict, tmp_path: Path) -> None:
    """A store changed after resolution cannot reach isolated Git transport."""
    create_managed_feature(git_env, "mutated-store-feature")
    write_managed_manifest(git_env["project"], [managed_record("mutated-store-feature")])
    alternates = git_env["project"] / ".git" / "objects" / "info" / "alternates"

    def mutate_before_push(*args, **kwargs):
        alternates.symlink_to(tmp_path / "missing-alternates")
        return None, None

    with (
        patch(
            "pynchy.host.git_ops.sync._managed_existing_pr",
            side_effect=mutate_before_push,
        ) as preflight,
        patch("pynchy.host.git_ops.sync.run_git") as git_runner,
        patch("pynchy.host.git_ops.sync._open_or_update_pr") as open_pr,
    ):
        result = host_create_pr_from_managed_feature("mutated-store-feature", [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature object store is unavailable.",
    }
    preflight.assert_called_once()
    git_runner.assert_not_called()
    open_pr.assert_not_called()
    assert "mutated-store-feature" not in git(git_env["origin"], "branch").stdout


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rejects_special_files_in_object_store(git_env: dict) -> None:
    """Managed Git never traverses special files from an agent-owned object store."""
    create_managed_feature(git_env, "special-store-feature")
    write_managed_manifest(git_env["project"], [managed_record("special-store-feature")])
    os.mkfifo(git_env["project"] / ".git" / "objects" / "unsafe-pipe")

    resolution = resolve_managed_feature_publication("special-store-feature", [git_env["repo_ctx"]])

    assert resolution.publication is None
    assert resolution.error == (
        "Publication blocked: configured repository 'owner/repo' object store is unavailable."
    )


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_recovers_when_pr_creation_races_with_existing_pr(git_env: dict) -> None:
    """A PR created concurrently still counts when its refs revalidate."""
    feature = "pr-create-race"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
    lookup_results = iter(
        (
            no_pr_result([]),
            no_pr_result([]),
            managed_pr_result([], base_sha=base_sha, head_sha=head_sha, branch_name=feature),
        )
    )

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr="exists"
                )
            return next(lookup_results)
        raise AssertionError(f"unexpected subprocess: {args}")

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": True,
        "message": f"Pushed 1 commit(s) to {feature}. PR updated: https://github.com/owner/repo/pull/1",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_reports_pr_creation_failure_when_raced_pr_has_wrong_head(git_env: dict) -> None:
    """A raced PR with different refs must not be treated as the requested PR."""
    feature = "pr-create-race-wrong-head"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    wrong_head = "f" * 40
    lookup_results = iter(
        (
            no_pr_result([]),
            no_pr_result([]),
            managed_pr_result([], base_sha=base_sha, head_sha=wrong_head, branch_name=feature),
        )
    )

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr="exists"
                )
            return next(lookup_results)
        raise AssertionError(f"unexpected subprocess: {args}")

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": f"Pushed 1 commit(s) to {feature}, but PR creation failed: exists",
    }
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() != wrong_head


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rejects_remote_ref_drift_before_opening_a_pr(git_env: dict) -> None:
    """A final remote-ref checkpoint blocks PR creation after a successful push."""
    feature = "remote-ref-drift"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync._managed_refs_match", return_value=False),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature refs changed after inspection.",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rejects_existing_pr_update_when_refs_drift_after_lookup(git_env: dict) -> None:
    """An existing PR is not reported updated after its final refs recheck fails."""
    feature = "existing-pr-ref-drift"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])

    with (
        patch(
            "pynchy.host.git_ops.sync._managed_existing_pr",
            side_effect=[
                (None, None),
                ("https://github.com/owner/repo/pull/1", None),
            ],
        ),
        patch(
            "pynchy.host.git_ops.sync._managed_refs_match",
            side_effect=[True, False],
        ),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature refs changed after inspection.",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rechecks_object_store_before_reading_commit_metadata(
    git_env: dict, tmp_path: Path
) -> None:
    """A store mutation after the ref check blocks isolated commit reads."""
    feature = "metadata-store-drift"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    alternates = git_env["project"] / ".git" / "objects" / "info" / "alternates"

    def drift_store(*_args: object, **_kwargs: object) -> bool:
        alternates.symlink_to(tmp_path / "missing-alternates")
        return True

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync._managed_refs_match", side_effect=drift_store),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature object store is unavailable.",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_updates_existing_approved_pr_without_creating_duplicate(git_env: dict) -> None:
    """An approved open PR is updated after its inspected head is pushed."""
    feature = "existing-approved-pr"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
    existing_pr = managed_pr_result([], base_sha=base_sha, head_sha=head_sha, branch_name=feature)
    gh_calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            gh_calls.append(args)
            return existing_pr
        raise AssertionError(f"unexpected subprocess: {args}")

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": True,
        "message": f"Pushed 1 commit(s) to {feature}. PR updated: https://github.com/owner/repo/pull/1",
    }
    assert len(gh_calls) == 2
    assert gh_calls[0][1:3] == ["pr", "list"]
    assert gh_calls[1][1:3] == ["pr", "list"]
