"""Tests for managed-feature resolver trust boundaries."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests model fixed Git subprocess results.
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest

from pynchy.host.git_ops.api import (
    RepoContext,
    host_create_pr_from_managed_feature,
    host_rebase_managed_feature,
    read_managed_feature_patch,
    resolve_managed_feature_publication,
)
from pynchy.host.git_ops.api import (
    run_git as host_run_git,
)
from tests.git_policy_support import (
    create_managed_feature,
    git,
    make_bare_origin,
    make_project,
    managed_record,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.action("lifecycle.managed.feature.publish")
class TestManagedFeatureResolution:
    """Resolver accepts only one active manifest-owned feature worktree."""

    def test_uses_remote_base_when_agent_rewrites_local_main(self, git_env: dict):
        """Cop inspection must not trust shared local remote-tracking refs."""
        remote_base = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        project = git_env["project"]
        (project / "malicious-base.txt").write_text("agent-only base change\n", encoding="utf-8")
        git(project, "add", "malicious-base.txt")
        git(project, "commit", "-m", "agent-local base commit")
        rewritten_main = git(project, "rev-parse", "HEAD").stdout.strip()
        git(project, "update-ref", "refs/remotes/origin/main", rewritten_main)
        git(project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

        create_managed_feature(git_env, "remote-base-feature")
        write_managed_manifest(project, [managed_record("remote-base-feature")])

        with patch(
            "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
            return_value=str(git_env["origin"]),
        ):
            resolution = resolve_managed_feature_publication(
                "remote-base-feature", [git_env["repo_ctx"]]
            )

        publication = resolution.publication
        assert publication is not None, resolution.error
        assert publication.base_sha == remote_base
        assert publication.base_sha != rewritten_main

        with patch(
            "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
            return_value=str(git_env["origin"]),
        ):
            patch_text, diagnostic = read_managed_feature_patch(publication)

        assert diagnostic is None
        assert patch_text is not None
        assert "+agent-only base change" in patch_text
        assert "+remote-base-feature change" in patch_text

    def test_rejects_feature_not_rebased_on_current_remote_base(self, git_env: dict):
        """Remote target advancement requires managed feature rebase."""
        project = git_env["project"]
        stale_base = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        worktree = create_managed_feature(git_env, "stale-base-feature")
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        write_managed_manifest(project, [managed_record("stale-base-feature")])

        (project / "advanced-base.txt").write_text("remote base advanced\n", encoding="utf-8")
        git(project, "add", "advanced-base.txt")
        git(project, "commit", "-m", "advance remote main")
        git(project, "push", "origin", "main")
        current_base = git(git_env["origin"], "rev-parse", "main").stdout.strip()

        with patch(
            "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
            return_value=str(git_env["origin"]),
        ):
            resolution = resolve_managed_feature_publication(
                "stale-base-feature", [git_env["repo_ctx"]]
            )

        assert stale_base != current_base
        assert feature_head != current_base
        assert resolution.publication is None
        assert resolution.error == (
            "Publication blocked: managed feature must be rebased on the remote "
            "default branch 'main'."
        )

    @pytest.mark.action("lifecycle.managed.feature.rebase")
    def test_rebases_stale_feature_onto_verified_remote_base(self, git_env: dict):
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
        resolution = resolve_managed_feature_publication(
            "stale-base-feature", [git_env["repo_ctx"]]
        )
        assert resolution.publication is not None, resolution.error

    @pytest.mark.parametrize(
        ("record", "version"),
        [
            (managed_record("inactive-feature", status="retired"), 2),
            (managed_record("bad-version-feature"), 1),
            (managed_record("absolute-feature", worktree="/absolute/feature"), 2),
            (managed_record("traversal-feature", worktree=".worktrees/../feature"), 2),
            (managed_record("wrong-path-feature", worktree=".worktrees/other"), 2),
        ],
    )
    def test_rejects_invalid_manifest_before_push(
        self,
        git_env: dict,
        record: dict[str, str],
        version: int,
    ) -> None:
        write_managed_manifest(git_env["project"], [record], version=version)
        with patch("pynchy.host.git_ops.sync.subprocess.run") as gh:
            result = host_create_pr_from_managed_feature(record["slug"], [git_env["repo_ctx"]])

        assert result["success"] is False
        gh.assert_not_called()
        assert record["branch"] not in git(git_env["origin"], "branch").stdout

    def test_rejects_stale_symlink_dirty_branch_mismatch_and_no_ahead(self, git_env: dict):
        project = git_env["project"]
        stale = managed_record("stale-feature")
        write_managed_manifest(project, [stale])
        assert (
            host_create_pr_from_managed_feature(stale["slug"], [git_env["repo_ctx"]])["success"]
            is False
        )

        symlink_worktree = create_managed_feature(git_env, "symlink-feature")
        git(project, "worktree", "remove", "--force", str(symlink_worktree))
        symlink_worktree.symlink_to(project, target_is_directory=True)
        write_managed_manifest(project, [managed_record("symlink-feature")])
        assert (
            host_create_pr_from_managed_feature("symlink-feature", [git_env["repo_ctx"]])["success"]
            is False
        )

        dirty_worktree = create_managed_feature(git_env, "dirty-feature")
        (dirty_worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        write_managed_manifest(project, [managed_record("dirty-feature")])
        assert (
            host_create_pr_from_managed_feature("dirty-feature", [git_env["repo_ctx"]])["success"]
            is False
        )

        create_managed_feature(git_env, "branch-feature")
        write_managed_manifest(
            project,
            [managed_record("branch-feature", branch="different-branch")],
        )
        assert (
            host_create_pr_from_managed_feature("branch-feature", [git_env["repo_ctx"]])["success"]
            is False
        )

        no_ahead = project / ".worktrees" / "no-ahead-feature"
        git(project, "worktree", "add", "-b", "no-ahead-feature", str(no_ahead), "main")
        write_managed_manifest(project, [managed_record("no-ahead-feature")])
        assert (
            host_create_pr_from_managed_feature("no-ahead-feature", [git_env["repo_ctx"]])[
                "success"
            ]
            is False
        )

    def test_rejects_target_common_directory_and_repository_ambiguity(
        self, git_env: dict, tmp_path: Path
    ):
        project = git_env["project"]
        create_managed_feature(git_env, "target-feature")
        write_managed_manifest(
            project,
            [managed_record("target-feature", target_branch="release")],
        )
        assert (
            host_create_pr_from_managed_feature("target-feature", [git_env["repo_ctx"]])["success"]
            is False
        )

        other_root = tmp_path / "other"
        other_root.mkdir()
        other_origin = make_bare_origin(other_root)
        foreign_worktree = project / ".worktrees" / "foreign-feature"
        git(tmp_path, "clone", str(other_origin), str(foreign_worktree))
        write_managed_manifest(project, [managed_record("foreign-feature")])
        assert (
            host_create_pr_from_managed_feature("foreign-feature", [git_env["repo_ctx"]])["success"]
            is False
        )

        second_root = tmp_path / "second"
        second_root.mkdir()
        second_project = make_project(second_root, git_env["origin"])
        second_ctx = RepoContext("owner/second", second_project, tmp_path / "second-worktrees")
        second_env = {"project": second_project}
        create_managed_feature(second_env, "ambiguous-feature")
        write_managed_manifest(second_project, [managed_record("ambiguous-feature")])
        create_managed_feature(git_env, "ambiguous-feature")
        write_managed_manifest(project, [managed_record("ambiguous-feature")])
        with patch("pynchy.host.git_ops.sync.subprocess.run") as gh:
            result = host_create_pr_from_managed_feature(
                "ambiguous-feature", [git_env["repo_ctx"], second_ctx]
            )

        assert result["success"] is False
        assert "ambiguous" in result["message"]
        gh.assert_not_called()

    @pytest.mark.parametrize("unsafe_store", ["objects", "info", "alternates", "pack"])
    def test_rejects_symlinked_or_alternate_object_store(
        self,
        git_env: dict,
        tmp_path: Path,
        unsafe_store: str,
    ) -> None:
        """Resolver never trusts Git object lookup indirection from a managed checkout."""
        project = git_env["project"]
        create_managed_feature(git_env, "unsafe-store-feature")
        write_managed_manifest(project, [managed_record("unsafe-store-feature")])
        object_dir = project / ".git" / "objects"
        if unsafe_store == "objects":
            moved_store = tmp_path / "moved-objects"
            object_dir.rename(moved_store)
            object_dir.symlink_to(moved_store, target_is_directory=True)
        elif unsafe_store == "info":
            info_dir = object_dir / "info"
            moved_info = tmp_path / "moved-info"
            info_dir.rename(moved_info)
            info_dir.symlink_to(moved_info, target_is_directory=True)
        elif unsafe_store == "pack":
            pack_dir = object_dir / "pack"
            pack_dir.mkdir(exist_ok=True)
            moved_pack = tmp_path / "moved-pack"
            pack_dir.rename(moved_pack)
            pack_dir.symlink_to(moved_pack, target_is_directory=True)
        else:
            alternate_store = tmp_path / "alternate-objects"
            alternate_store.mkdir()
            (object_dir / "info" / "alternates").write_text(
                f"{alternate_store}\n",
                encoding="utf-8",
            )

        resolution = resolve_managed_feature_publication(
            "unsafe-store-feature", [git_env["repo_ctx"]]
        )

        assert resolution.publication is None
        assert resolution.error == (
            "Publication blocked: configured repository 'owner/repo' object store is unavailable."
        )

    def test_rechecks_broken_alternate_symlink_before_cop_patch(
        self, git_env: dict, tmp_path: Path
    ) -> None:
        """A store mutation after resolution cannot redirect Cop's object lookup."""
        project = git_env["project"]
        create_managed_feature(git_env, "rechecked-store-feature")
        write_managed_manifest(project, [managed_record("rechecked-store-feature")])
        resolution = resolve_managed_feature_publication(
            "rechecked-store-feature", [git_env["repo_ctx"]]
        )
        publication = resolution.publication
        assert publication is not None, resolution.error
        alternates = project / ".git" / "objects" / "info" / "alternates"
        alternates.symlink_to(tmp_path / "missing-alternates")

        patch_text, diagnostic = read_managed_feature_patch(publication)

        assert patch_text is None
        assert diagnostic == (
            "Publication blocked: configured repository 'owner/repo' object store is unavailable."
        )

    def test_rejects_noncanonical_and_inactive_features(self, git_env: dict) -> None:
        invalid = resolve_managed_feature_publication("Not a slug", [git_env["repo_ctx"]])
        assert invalid.publication is None
        assert invalid.error == (
            "Publication blocked: feature_slug must be a canonical managed-feature slug."
        )

        inactive = resolve_managed_feature_publication("missing-feature", [git_env["repo_ctx"]])
        assert inactive.publication is None
        assert inactive.error == (
            "Publication blocked: managed feature 'missing-feature' is not active in a "
            "configured repository."
        )

    def test_rejects_unverifiable_head_and_object_format(self, git_env: dict) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "invalid-git-feature")
        write_managed_manifest(project, [managed_record("invalid-git-feature")])

        with patch(
            "pynchy.host.git_ops.managed_feature.run_git",
            return_value=subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=1, stdout="", stderr="broken head"
            ),
        ):
            invalid_head = resolve_managed_feature_publication(
                "invalid-git-feature", [git_env["repo_ctx"]]
            )
        assert invalid_head.publication is None
        assert invalid_head.error == (
            "Publication blocked: could not verify HEAD for managed feature 'invalid-git-feature'."
        )

        with patch(
            "pynchy.host.git_ops.managed_feature.run_git",
            side_effect=[
                subprocess.CompletedProcess(
                    args=["git", "rev-parse"],
                    returncode=0,
                    stdout="a" * 40,
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=["git", "rev-parse"], returncode=1, stdout="", stderr="unknown format"
                ),
            ],
        ):
            invalid_format = resolve_managed_feature_publication(
                "invalid-git-feature", [git_env["repo_ctx"]]
            )
        assert invalid_format.publication is None
        assert invalid_format.error == (
            "Publication blocked: could not verify Git object format for 'invalid-git-feature'."
        )

    def test_rejects_remote_metadata_and_commit_count_failures(self, git_env: dict) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "remote-metadata-feature")
        write_managed_manifest(project, [managed_record("remote-metadata-feature")])

        with patch("pynchy.host.git_ops.managed_feature._remote_default_branch", return_value=None):
            missing_default = resolve_managed_feature_publication(
                "remote-metadata-feature", [git_env["repo_ctx"]]
            )
        assert missing_default.publication is None
        assert missing_default.error == (
            "Publication blocked: managed feature 'remote-metadata-feature' targets 'main', "
            "not the configured remote default branch."
        )

        with patch("pynchy.host.git_ops.managed_feature._fetch_remote_ref", return_value=None):
            missing_base = resolve_managed_feature_publication(
                "remote-metadata-feature", [git_env["repo_ctx"]]
            )
        assert missing_base.publication is None
        assert missing_base.error == (
            "Publication blocked: could not verify base for managed feature "
            "'remote-metadata-feature'."
        )

        with patch("pynchy.host.git_ops.managed_feature._count_raw_commits", return_value=None):
            missing_count = resolve_managed_feature_publication(
                "remote-metadata-feature", [git_env["repo_ctx"]]
            )
        assert missing_count.publication is None
        assert missing_count.error == (
            "Publication blocked: could not verify commits for managed feature "
            "'remote-metadata-feature'."
        )

    @pytest.mark.parametrize("count_result", [(1, ""), (0, "not-an-integer")])
    def test_rejects_unreadable_commit_count(
        self, git_env: dict, count_result: tuple[int, str]
    ) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "unreadable-count-feature")
        write_managed_manifest(project, [managed_record("unreadable-count-feature")])

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "rev-list" in args:
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=count_result[0],
                    stdout=count_result[1],
                    stderr="",
                )
            return host_run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.managed_feature.run_git", side_effect=fake_run_git):
            result = resolve_managed_feature_publication(
                "unreadable-count-feature", [git_env["repo_ctx"]]
            )

        assert result.publication is None
        assert result.error == (
            "Publication blocked: could not verify commits for managed feature "
            "'unreadable-count-feature'."
        )

    def test_rejects_failed_isolated_git_initialization(self, git_env: dict) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "isolated-init-feature")
        write_managed_manifest(project, [managed_record("isolated-init-feature")])

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:1] == ("init",):
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=1, stdout="", stderr=""
                )
            return host_run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.managed_feature.run_git", side_effect=fake_run_git):
            result = resolve_managed_feature_publication(
                "isolated-init-feature", [git_env["repo_ctx"]]
            )

        assert result.publication is None
        assert result.error == (
            "Publication blocked: could not initialize isolated Git for 'owner/repo'."
        )

    @pytest.mark.parametrize(
        ("remote_mode", "expected"),
        [
            (
                "failure",
                (
                    "Publication blocked: managed feature 'remote-parse-feature' targets 'main', "
                    "not the configured remote default branch."
                ),
            ),
            (
                "wrong-branch",
                (
                    "Publication blocked: managed feature 'remote-parse-feature' targets 'main', "
                    "not the configured remote default branch."
                ),
            ),
        ],
    )
    def test_rejects_unusable_remote_default_branch(
        self, git_env: dict, remote_mode: str, expected: str
    ) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "remote-parse-feature")
        write_managed_manifest(project, [managed_record("remote-parse-feature")])

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "--symref" in args:
                if remote_mode == "failure":
                    return subprocess.CompletedProcess(
                        args=["git", *args], returncode=1, stdout="", stderr=""
                    )
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=0,
                    stdout="not-a-head-ref\n",
                    stderr="",
                )
            return host_run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.managed_feature.run_git", side_effect=fake_run_git):
            result = resolve_managed_feature_publication(
                "remote-parse-feature", [git_env["repo_ctx"]]
            )

        assert result.publication is None
        assert result.error == expected

    @pytest.mark.parametrize(
        "remote_output",
        [
            "",
            "not-a-sha\trefs/heads/main\n",
            "a" * 40 + "\trefs/heads/release\n",
            ("a" * 40 + "\trefs/heads/main\n") * 2,
        ],
    )
    def test_rejects_malformed_remote_base_record(self, git_env: dict, remote_output: str) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "remote-base-parse-feature")
        write_managed_manifest(project, [managed_record("remote-base-parse-feature")])

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "--refs" in args:
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=0, stdout=remote_output, stderr=""
                )
            return host_run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.managed_feature.run_git", side_effect=fake_run_git):
            result = resolve_managed_feature_publication(
                "remote-base-parse-feature", [git_env["repo_ctx"]]
            )

        assert result.publication is None
        assert result.error == (
            "Publication blocked: could not verify base for managed feature "
            "'remote-base-parse-feature'."
        )

    @pytest.mark.parametrize("fetch_mode", ["fetch-failed", "resolved-sha-mismatch"])
    def test_rejects_remote_base_fetch_integrity_failure(
        self, git_env: dict, fetch_mode: str
    ) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "remote-fetch-integrity-feature")
        write_managed_manifest(project, [managed_record("remote-fetch-integrity-feature")])

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if fetch_mode == "fetch-failed" and "fetch" in args:
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=1, stdout="", stderr=""
                )
            if fetch_mode == "resolved-sha-mismatch" and "refs/pynchy/managed-base" in args:
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=0, stdout=("b" * 40) + "\n", stderr=""
                )
            return host_run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.managed_feature.run_git", side_effect=fake_run_git):
            result = resolve_managed_feature_publication(
                "remote-fetch-integrity-feature", [git_env["repo_ctx"]]
            )

        assert result.publication is None
        assert result.error == (
            "Publication blocked: could not verify base for managed feature "
            "'remote-fetch-integrity-feature'."
        )

    @pytest.mark.parametrize(
        ("command", "returncode", "expected"),
        [
            (
                "read-tree",
                1,
                (
                    "Publication blocked: could not inspect managed feature "
                    "'status-read-tree-feature' status."
                ),
            ),
            (
                "update-index",
                2,
                (
                    "Publication blocked: could not inspect managed feature "
                    "'status-update-index-feature' status."
                ),
            ),
            (
                "diff-index",
                1,
                (
                    "Publication blocked: managed feature has uncommitted changes. "
                    "Commit all changes first."
                ),
            ),
            (
                "diff-index",
                2,
                (
                    "Publication blocked: could not inspect managed feature "
                    "'status-diff-index-feature' status."
                ),
            ),
            (
                "ls-files",
                1,
                (
                    "Publication blocked: could not inspect managed feature "
                    "'status-ls-files-feature' status."
                ),
            ),
        ],
    )
    def test_rejects_unreadable_managed_worktree_status(
        self,
        git_env: dict,
        command: str,
        returncode: int,
        expected: str,
    ) -> None:
        feature_slug = f"status-{command}-feature"
        create_managed_feature(git_env, feature_slug)
        write_managed_manifest(git_env["project"], [managed_record(feature_slug)])

        def fake_run_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command in args:
                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=returncode, stdout="", stderr=""
                )
            return host_run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.managed_feature.run_git", side_effect=fake_run_git):
            result = resolve_managed_feature_publication(feature_slug, [git_env["repo_ctx"]])

        assert result.publication is None
        assert result.error == expected

    def test_revalidates_remote_base_and_redacts_patch_failure(self, git_env: dict) -> None:
        project = git_env["project"]
        create_managed_feature(git_env, "patch-revalidation-feature")
        write_managed_manifest(project, [managed_record("patch-revalidation-feature")])
        resolution = resolve_managed_feature_publication(
            "patch-revalidation-feature", [git_env["repo_ctx"]]
        )
        publication = resolution.publication
        assert publication is not None, resolution.error

        redacted_url = "https://user:secret@example.com"  # pragma: allowlist secret
        with patch(
            "pynchy.host.git_ops.managed_feature.run_git_bounded_stdout",
            return_value=Mock(
                returncode=1,
                stdout="",
                stderr=f"fatal: {redacted_url} rejected",
                exceeded_limit=False,
            ),
        ):
            patch_text, diagnostic = read_managed_feature_patch(publication)
        assert patch_text is None
        assert diagnostic is not None
        assert "secret" not in diagnostic
        assert "https://***@example.com" in diagnostic

        with patch(
            "pynchy.host.git_ops.managed_feature.run_git_bounded_stdout",
            return_value=Mock(returncode=0, stdout="partial", stderr="", exceeded_limit=True),
        ):
            patch_text, diagnostic = read_managed_feature_patch(publication)
        assert patch_text is None
        assert diagnostic == "Committed patch exceeds the Cop inspection context limit"

        (project / "remote-change.txt").write_text("remote change\n", encoding="utf-8")
        git(project, "add", "remote-change.txt")
        git(project, "commit", "-m", "advance remote target")
        git(project, "push", "origin", "main")
        patch_text, diagnostic = read_managed_feature_patch(publication)
        assert patch_text is None
        assert diagnostic == "managed feature target changed after inspection"

    def test_bounded_patch_reader_stops_on_oversized_committed_diff(self, git_env: dict) -> None:
        worktree = create_managed_feature(git_env, "oversized-real-patch-feature")
        (worktree / "large.txt").write_text("x" * 70_000, encoding="utf-8")
        git(worktree, "add", "large.txt")
        git(worktree, "commit", "-m", "add large patch")
        write_managed_manifest(git_env["project"], [managed_record("oversized-real-patch-feature")])

        resolution = resolve_managed_feature_publication(
            "oversized-real-patch-feature", [git_env["repo_ctx"]]
        )
        publication = resolution.publication
        assert publication is not None, resolution.error

        patch_text, diagnostic = read_managed_feature_patch(publication)

        assert patch_text is None
        assert diagnostic == "Committed patch exceeds the Cop inspection context limit"

    def test_bounded_patch_reader_returns_a_small_committed_diff(self, git_env: dict) -> None:
        worktree = create_managed_feature(git_env, "small-real-patch-feature")
        (worktree / "small.txt").write_text("small change\n", encoding="utf-8")
        git(worktree, "add", "small.txt")
        git(worktree, "commit", "-m", "add small patch")
        write_managed_manifest(git_env["project"], [managed_record("small-real-patch-feature")])

        resolution = resolve_managed_feature_publication(
            "small-real-patch-feature", [git_env["repo_ctx"]]
        )
        publication = resolution.publication
        assert publication is not None, resolution.error

        patch_text, diagnostic = read_managed_feature_patch(publication)

        assert diagnostic is None
        assert patch_text is not None
        assert "+small change" in patch_text
