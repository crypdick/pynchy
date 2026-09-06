"""Tests for rebasing managed feature branches on their verified remote base."""

from __future__ import annotations

import subprocess  # noqa: S404 - models fixed Git subprocess results.
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from pynchy.host.git_ops.api import (
    ManagedFeaturePublication,
    ManagedFeatureResolution,
    RepoContext,
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


def _publication(tmp_path) -> ManagedFeaturePublication:
    repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "worktrees")
    return ManagedFeaturePublication(
        repo_ctx=repo_ctx,
        feature_slug="safe-feature",
        worktree_path=tmp_path / "feature",
        branch_name="safe-feature",
        main_branch="main",
        remote_url="https://example.test/owner/repo.git",
        base_sha="b" * 40,
        head_sha="a" * 40,
        object_format="sha1",
        ahead=1,
        git_common_dir=tmp_path / ".git",
    )


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


@pytest.mark.action("lifecycle.managed.feature.rebase")
@pytest.mark.parametrize(
    ("resolution", "head_current", "prepared", "expected"),
    [
        (
            ManagedFeatureResolution(None, "not manifest-bound"),
            True,
            None,
            {"success": False, "message": "not manifest-bound"},
        ),
        (
            ManagedFeatureResolution(None, None),
            True,
            None,
            {"success": False, "message": "Rebase blocked."},
        ),
        (
            "publication",
            False,
            None,
            {
                "success": False,
                "message": "Rebase blocked: managed feature changed during validation. Retry.",
            },
        ),
        (
            "publication",
            True,
            {"success": False, "message": "remote base changed"},
            {"success": False, "message": "remote base changed"},
        ),
        (
            "publication",
            True,
            subprocess.CompletedProcess([], 0, "", ""),
            {
                "success": True,
                "message": (
                    "Rebased managed feature 'safe-feature' onto remote default branch 'main'."
                ),
            },
        ),
    ],
)
def test_reports_public_rebase_outcomes(
    tmp_path,
    resolution: ManagedFeatureResolution | str,
    head_current: bool,
    prepared: dict[str, object] | subprocess.CompletedProcess[str] | None,
    expected: dict[str, object],
) -> None:
    publication = _publication(tmp_path)
    resolved = (
        ManagedFeatureResolution(publication, None) if resolution == "publication" else resolution
    )
    with (
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._resolve_managed_feature",
            return_value=resolved,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._managed_feature_head_is_current",
            return_value=head_current,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._prepare_rebase",
            return_value=prepared,
        ) as prepare,
    ):
        result = host_rebase_managed_feature("safe-feature", [publication.repo_ctx])

    assert result == expected
    if prepared is None:
        prepare.assert_not_called()


@pytest.mark.action("lifecycle.managed.feature.rebase")
def test_reports_manifest_revalidation_failure(tmp_path) -> None:
    publication = _publication(tmp_path)
    with (
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._resolve_managed_feature",
            return_value=ManagedFeatureResolution(publication, None),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._managed_feature_head_is_current",
            return_value=True,
        ),
        patch("pynchy.host.git_ops.managed_feature_rebase._ManifestValidationError", ValueError),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._prepare_rebase",
            side_effect=ValueError("unsafe Git metadata"),
        ),
    ):
        result = host_rebase_managed_feature("safe-feature", [publication.repo_ctx])

    assert result == {"success": False, "message": "unsafe Git metadata"}


@pytest.mark.action("lifecycle.managed.feature.rebase")
@pytest.mark.parametrize(
    ("current_base", "head_descends", "persisted", "expected"),
    [
        (
            "changed",
            False,
            True,
            "Rebase blocked: remote default branch changed during validation. Retry.",
        ),
        (
            "verified",
            True,
            True,
            "Managed feature 'safe-feature' already includes remote default branch 'main'.",
        ),
        (
            "verified",
            False,
            False,
            "Rebase blocked: could not prepare the verified remote base.",
        ),
    ],
)
def test_reports_rebase_preparation_outcomes(
    tmp_path,
    current_base: str,
    head_descends: bool,
    persisted: bool,
    expected: str,
) -> None:
    publication = _publication(tmp_path)
    transport = MagicMock()
    fetched = publication.base_sha if current_base == "verified" else "c" * 40
    with (
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._resolve_managed_feature",
            return_value=ManagedFeatureResolution(publication, None),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._managed_feature_head_is_current",
            return_value=True,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._isolated_managed_git",
            return_value=nullcontext(transport),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._fetch_remote_ref",
            return_value=fetched,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._head_descends_from",
            return_value=head_descends,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._persist_verified_base",
            return_value=persisted,
        ),
    ):
        result = host_rebase_managed_feature("safe-feature", [publication.repo_ctx])

    assert result == {"success": current_base == "verified" and head_descends, "message": expected}


@pytest.mark.action("lifecycle.managed.feature.rebase")
@pytest.mark.parametrize("failure", ["spawn", "missing-pipe", "index"])
def test_reports_verified_base_copy_failures(tmp_path, failure: str) -> None:
    publication = _publication(tmp_path)
    source = MagicMock()
    source.stdin = None if failure == "missing-pipe" else BytesIO()
    source.stdout = BytesIO()
    popen_error = OSError("git unavailable") if failure == "spawn" else None
    index_error = OSError("index failed") if failure == "index" else None
    with (
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._ManagedGitTransport", autospec=True
        ) as transport_type,
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._resolve_managed_feature",
            return_value=ManagedFeatureResolution(publication, None),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._managed_feature_head_is_current",
            return_value=True,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._isolated_managed_git",
            return_value=nullcontext(transport_type.return_value),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._fetch_remote_ref",
            return_value=publication.base_sha,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._head_descends_from",
            return_value=False,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase.subprocess.Popen",
            return_value=source,
            side_effect=popen_error,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
            side_effect=index_error,
        ),
    ):
        transport_type.return_value.args = ()
        transport_type.return_value.root = tmp_path
        result = host_rebase_managed_feature("safe-feature", [publication.repo_ctx])

    assert result == {
        "success": False,
        "message": "Rebase blocked: could not prepare the verified remote base.",
    }


@pytest.mark.action("lifecycle.managed.feature.rebase")
@pytest.mark.parametrize(
    ("rebase_active", "stderr", "expected"),
    [
        (
            True,
            "",
            {
                "success": False,
                "message": (
                    "Managed feature rebase has conflicts. Resolve them, then run "
                    "git rebase --continue or git rebase --abort."
                ),
            },
        ),
        (False, "fatal: rebase failed", {"success": False, "message": "Rebase blocked: redacted"}),
    ],
)
def test_reports_rebase_failure_outcomes(tmp_path, rebase_active, stderr, expected) -> None:
    publication = _publication(tmp_path)
    state_path = tmp_path / "rebase-merge"
    if rebase_active:
        state_path.touch()
    state = subprocess.CompletedProcess(
        [],
        0 if rebase_active else 1,
        f"{state_path}\n" if rebase_active else "",
        "",
    )
    with (
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._resolve_managed_feature",
            return_value=ManagedFeatureResolution(publication, None),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._managed_feature_head_is_current",
            return_value=True,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase._prepare_rebase",
            return_value=subprocess.CompletedProcess([], 1, "", stderr),
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase.run_git",
            return_value=state,
        ),
        patch(
            "pynchy.host.git_ops.managed_feature_rebase.redact_git_diagnostic",
            return_value="redacted",
        ) as redact,
    ):
        result = host_rebase_managed_feature("safe-feature", [publication.repo_ctx])

    assert result == expected
    if rebase_active:
        redact.assert_not_called()
