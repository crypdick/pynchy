"""Tests for rebasing managed feature branches on their verified remote base."""

from __future__ import annotations

import pytest

from pynchy.host.git_ops.api import (
    host_rebase_managed_feature,
    resolve_managed_feature_publication,
)
from tests.git_policy_support import (
    create_managed_feature,
    git,
    managed_record,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)


@pytest.mark.action("lifecycle.managed.feature.rebase")
def test_rebases_stale_feature_onto_verified_remote_base(git_env: dict) -> None:
    project = git_env["project"]
    worktree = create_managed_feature(git_env, "stale-base-feature")
    write_managed_manifest(project, [managed_record("stale-base-feature")])

    (project / "advanced-base.txt").write_text("remote base advanced\n", encoding="utf-8")
    git(project, "add", "advanced-base.txt")
    git(project, "commit", "-m", "advance remote main")
    git(project, "push", "origin", "main")
    current_base = git(git_env["origin"], "rev-parse", "main").stdout.strip()

    result = host_rebase_managed_feature("stale-base-feature", [git_env["repo_ctx"]])

    assert result == {
        "success": True,
        "message": (
            "Rebased managed feature 'stale-base-feature' onto remote default branch 'main'."
        ),
    }
    assert git(worktree, "merge-base", "--is-ancestor", current_base, "HEAD").returncode == 0
    resolution = resolve_managed_feature_publication("stale-base-feature", [git_env["repo_ctx"]])
    assert resolution.publication is not None, resolution.error
