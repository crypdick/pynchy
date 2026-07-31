"""Tests for managed-feature manifest and path validation failures."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests model fixed Git subprocess results.
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import RepoContext, resolve_managed_feature_publication
from tests.git_policy_support import (
    create_managed_feature,
    managed_record,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.action("lifecycle.managed.feature.publish")
class TestManagedFeatureManifestEdges:
    """Manifest and Git identity failures stay bounded and opaque."""

    @pytest.mark.parametrize(
        ("manifest", "expected"),
        [
            (
                "not valid = [",
                "Publication blocked: managed-feature manifest for 'owner/repo' is unreadable.",
            ),
            (
                "version = 2\nfeatures = []\n",
                (
                    "Publication blocked: managed feature 'missing-feature' is not active in a "
                    "configured repository."
                ),
            ),
            (
                "version = 2\n[features.other_feature]\nslug = 'other-feature'\n",
                (
                    "Publication blocked: managed feature 'missing-feature' is not active in a "
                    "configured repository."
                ),
            ),
        ],
    )
    def test_rejects_unusable_manifest_records(
        self, git_env: dict, manifest: str, expected: str
    ) -> None:
        manifest_path = git_env["project"] / ".new-feature" / "manifest.toml"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest, encoding="utf-8")

        result = resolve_managed_feature_publication("missing-feature", [git_env["repo_ctx"]])

        assert result.publication is None
        assert result.error == expected

    def test_rejects_duplicate_manifest_identity(self, git_env: dict) -> None:
        manifest_path = git_env["project"] / ".new-feature" / "manifest.toml"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            """
version = 2

[features.duplicate_feature]
slug = "duplicate-feature"

[features.other_feature]
slug = "duplicate-feature"
""",
            encoding="utf-8",
        )

        result = resolve_managed_feature_publication("duplicate-feature", [git_env["repo_ctx"]])

        assert result.publication is None
        assert result.error == (
            "Publication blocked: managed feature 'duplicate-feature' has an invalid manifest "
            "identity."
        )

    @pytest.mark.parametrize("branch", ["", "bad..branch"])
    def test_rejects_missing_or_invalid_manifest_branch(self, git_env: dict, branch: str) -> None:
        write_managed_manifest(
            git_env["project"],
            [managed_record("invalid-branch-feature", branch=branch)],
        )

        result = resolve_managed_feature_publication(
            "invalid-branch-feature", [git_env["repo_ctx"]]
        )

        assert result.publication is None
        assert result.error == (
            "Publication blocked: managed feature manifest for 'owner/repo' has no valid branch."
        )

    def test_rejects_unavailable_configured_repository(self, git_env: dict, tmp_path: Path) -> None:
        missing_ctx = RepoContext("owner/missing", tmp_path / "missing", tmp_path / "worktrees")

        result = resolve_managed_feature_publication("missing-feature", [missing_ctx])

        assert result.publication is None
        assert (
            result.error
            == "Publication blocked: configured repository 'owner/missing' is unavailable."
        )

    def test_rejects_non_git_managed_worktree(self, git_env: dict) -> None:
        worktree = git_env["project"] / ".worktrees" / "non-git-feature"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("not a gitdir", encoding="utf-8")
        write_managed_manifest(git_env["project"], [managed_record("non-git-feature")])

        result = resolve_managed_feature_publication("non-git-feature", [git_env["repo_ctx"]])

        assert result.publication is None
        assert result.error == (
            "Publication blocked: managed feature 'non-git-feature' worktree does not match its "
            "manifest."
        )

    def test_rejects_git_identity_when_path_resolution_races(
        self, git_env: dict, tmp_path: Path
    ) -> None:
        worktree = create_managed_feature(git_env, "raced-path-feature")
        write_managed_manifest(git_env["project"], [managed_record("raced-path-feature")])
        missing_path = tmp_path / "gone-before-resolution"

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=0, stdout=f"{missing_path}\n", stderr=""
                )
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=0, stdout="", stderr=""
            )

        with patch(
            "pynchy.host.git_ops.managed_feature_manifest.run_git", side_effect=fake_run_git
        ):
            result = resolve_managed_feature_publication(
                "raced-path-feature", [git_env["repo_ctx"]]
            )

        assert worktree.exists()
        assert result.publication is None
        assert result.error == (
            "Publication blocked: managed feature 'raced-path-feature' worktree does not match "
            "its manifest."
        )
