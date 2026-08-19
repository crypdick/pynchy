"""Regression tests for managed alternate-object transport revalidation."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - test double models fixed gh subprocess responses.
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

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
def test_blocks_publication_when_isolated_transport_disappears(
    git_env: dict,
) -> None:
    feature = "missing-isolated-transport"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])

    def remove_transport(
        _ctx: object, isolated_dir: Path, *_args: object, **_kwargs: object
    ) -> None:
        transport = isolated_dir / "repository.git"
        if transport.exists():
            if transport.is_dir():
                shutil.rmtree(transport)
            else:
                transport.unlink()

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync._push_managed_feature", side_effect=remove_transport),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature Git identity is incomplete.",
    }


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
        "pr_url": "https://github.com/owner/repo/pull/1",
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
@pytest.mark.parametrize("mutation_point", ["init", "target", "fetch", "base", "remote"])
def test_rechecks_object_store_between_isolated_transport_commands(
    git_env: dict, tmp_path: Path, mutation_point: str
) -> None:
    """Every isolated Git command revalidates the agent-owned object store."""
    feature = f"transport-store-{mutation_point}"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    alternates = git_env["project"] / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(exist_ok=True)
    real_run_git = run_git

    def mutate_after_command(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = real_run_git(*args, **kwargs)
        matches = {
            "init": args[:2] == ("init", "--bare"),
            "target": args[-1:] == ("refs/heads/main",),
            "fetch": "fetch" in args,
            "base": args[-1:] == ("refs/pynchy/managed-base",),
            "remote": args[-1:] == (f"refs/heads/{feature}",),
        }
        if matches[mutation_point]:
            alternates.symlink_to(tmp_path / f"missing-{mutation_point}")
        return result

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync.run_git", side_effect=mutate_after_command),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature object store is unavailable.",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
@pytest.mark.parametrize(
    "remote_output",
    [
        "not-a-sha\trefs/heads/main\n",
        f"{'a' * 40}\trefs/heads/main\n{'b' * 40}\trefs/heads/main\n",
    ],
)
def test_rejects_malformed_final_managed_ref_response(git_env: dict, remote_output: str) -> None:
    """A malformed final remote-ref response cannot authorize PR publication."""
    feature = "malformed-final-refs"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    real_run_git = run_git

    def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[-2:] == ("refs/heads/main", f"refs/heads/{feature}"):
            return subprocess.CompletedProcess(args, 0, remote_output, "")
        return real_run_git(*args, **kwargs)

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync.run_git", side_effect=fake_run_git),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature refs changed after inspection.",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_rejects_fetched_managed_base_sha_mismatch(git_env: dict) -> None:
    """A fetched base ref that resolves to another SHA blocks publication."""
    feature = "mismatched-fetched-base"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    real_run_git = run_git

    def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[-1:] == ("refs/pynchy/managed-base",):
            return subprocess.CompletedProcess(args, 0, f"{'f' * 40}\n", "")
        return real_run_git(*args, **kwargs)

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync.run_git", side_effect=fake_run_git),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": "Publication blocked: managed feature target changed after Cop inspection.",
    }


@pytest.mark.action("lifecycle.managed.feature.publish")
@pytest.mark.parametrize("metadata_phase", ["title", "body"])
def test_rechecks_object_store_after_each_commit_metadata_read(
    git_env: dict, tmp_path: Path, metadata_phase: str
) -> None:
    """Commit metadata reads never proceed after the store changes."""
    feature = f"metadata-read-{metadata_phase}"
    create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    alternates = git_env["project"] / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(exist_ok=True)

    def bounded_git(*args: str, **_kwargs: object) -> object:
        phase = "title" if "-1" in args else "body"
        if phase == metadata_phase:
            alternates.symlink_to(tmp_path / f"missing-{metadata_phase}")
        result = MagicMock()
        result.returncode = 0
        result.stdout = phase
        result.exceeded_limit = False
        return result

    with (
        patch("pynchy.host.git_ops.sync._managed_existing_pr", return_value=(None, None)),
        patch("pynchy.host.git_ops.sync.run_git_bounded_stdout", side_effect=bounded_git),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    expected_message = (
        "Publication blocked: managed feature object store is unavailable."
        if metadata_phase == "title"
        else "Publication blocked: managed feature refs changed after inspection."
    )
    assert result == {"success": False, "message": expected_message}


@pytest.mark.action("lifecycle.managed.feature.publish")
def test_closes_created_pr_when_final_ref_check_drifts(git_env: dict) -> None:
    """A post-creation ref race is reported and compensated with PR closure."""
    feature = "created-pr-ref-drift"
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
    real_subprocess_run = subprocess.run
    gh_calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            gh_calls.append(args)
            if args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args, 0, "https://github.com/owner/repo/pull/1\n", ""
                )
            if args[1:3] == ["pr", "close"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return next(lookup_results)
        return real_subprocess_run(args, check=kwargs.pop("check", False), **kwargs)

    with (
        patch(
            "pynchy.host.git_ops.sync._managed_refs_match",
            side_effect=[True, True, False],
        ),
        patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_run),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {
        "success": False,
        "message": (
            "Publication blocked: managed feature refs changed after inspection. "
            "The host closed the newly created PR."
        ),
    }
    assert [call[1:3] for call in gh_calls].count(["pr", "close"]) == 1


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
        "pr_url": "https://github.com/owner/repo/pull/1",
        "message": f"Pushed 1 commit(s) to {feature}. PR updated: https://github.com/owner/repo/pull/1",
    }
    assert len(gh_calls) == 2
    assert gh_calls[0][1:3] == ["pr", "list"]
    assert gh_calls[1][1:3] == ["pr", "list"]
