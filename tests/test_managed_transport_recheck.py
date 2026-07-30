"""Regression tests for managed alternate-object transport revalidation."""

from __future__ import annotations

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
    managed_record,
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
