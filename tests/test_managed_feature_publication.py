"""Tests for bounded pull-request publication of managed features."""

from __future__ import annotations

import shlex
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import RepoContext, host_create_pr_from_managed_feature, run_git
from tests.git_policy_support import (
    create_managed_feature,
    git,
    make_bare_origin,
    make_project,
    managed_record,
    replace_head_with_signed_commit,
    write_managed_manifest,
)
from tests.git_policy_support import managed_pr_result as _managed_pr_result
from tests.git_policy_support import no_pr_result as _no_pr_result

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.action("lifecycle.managed.feature.publish")
class TestManagedFeaturePublication:
    """Managed feature publication stays bound to one manifest record."""

    def test_rejects_a_stale_inspected_head_before_publishing(self, git_env: dict) -> None:
        create_managed_feature(git_env, "stale-head-feature")
        write_managed_manifest(git_env["project"], [managed_record("stale-head-feature")])

        result = host_create_pr_from_managed_feature(
            "stale-head-feature",
            [git_env["repo_ctx"]],
            expected_head_sha="0" * 40,
        )

        assert result == {
            "success": False,
            "message": (
                "Publication blocked: managed feature changed after Cop inspection. "
                "Inspect and publish it again."
            ),
        }

    def test_pushes_only_selected_manifest_feature(self, git_env: dict, tmp_path: Path):
        selected = create_managed_feature(git_env, "selected-feature")
        (selected / ".gitattributes").write_text(
            "selected-feature.txt filter=hostile\n",
            encoding="utf-8",
        )
        git(selected, "add", ".gitattributes")
        git(selected, "commit", "-m", "configure selected feature attributes")
        create_managed_feature(git_env, "other-feature")
        write_managed_manifest(
            git_env["project"],
            [managed_record("selected-feature"), managed_record("other-feature")],
        )
        hostile_root = tmp_path / "hostile"
        hostile_root.mkdir()
        hostile_origin = make_bare_origin(hostile_root)
        trusted_url = git_env["origin"].as_uri()
        hostile_url = hostile_origin.as_uri()
        git(git_env["project"], "remote", "set-url", "origin", hostile_url)
        git(
            git_env["project"],
            "config",
            "--add",
            f"url.{hostile_url}.pushInsteadOf",
            trusted_url,
        )
        hostile_hook = git_env["project"] / ".git" / "hooks" / "pre-push"
        hostile_hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hostile_hook.chmod(0o755)
        fsmonitor_marker = tmp_path / "fsmonitor-ran"
        fsmonitor = tmp_path / "hostile-fsmonitor"
        fsmonitor.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(fsmonitor_marker))}\n",
            encoding="utf-8",
        )
        fsmonitor.chmod(0o755)
        git(git_env["project"], "config", "core.fsmonitor", str(fsmonitor))
        filter_marker = tmp_path / "filter-ran"
        clean_filter = tmp_path / "hostile-filter"
        clean_filter.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(filter_marker))}\ncat\n",
            encoding="utf-8",
        )
        clean_filter.chmod(0o755)
        git(git_env["project"], "config", "filter.hostile.clean", str(clean_filter))
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        selected_head = git(selected, "rev-parse", "HEAD").stdout.strip()
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            _no_pr_result([]),
            _no_pr_result([]),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
            _managed_pr_result(
                [], base_sha=base_sha, head_sha=selected_head, branch_name="selected-feature"
            ),
        ]
        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=trusted_url,
            ),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature("selected-feature", [git_env["repo_ctx"]])

        assert result["success"] is True
        assert selected.exists()
        assert "selected-feature" in git(git_env["origin"], "branch").stdout
        assert "selected-feature" not in git(hostile_origin, "branch").stdout
        assert "other-feature" not in git(git_env["origin"], "branch").stdout
        assert not fsmonitor_marker.exists()
        assert not filter_marker.exists()
        assert gh_calls[2][gh_calls[2].index("--base") : gh_calls[2].index("--head") + 2] == [
            "--base",
            "main",
            "--head",
            "selected-feature",
        ]
        assert gh_calls[2][gh_calls[2].index("--title") + 1] == (
            "configure selected feature attributes"
        )
        assert gh_calls[2][gh_calls[2].index("--body") + 1] == (
            "Automated PR from managed feature `selected-feature`.\n\n### Commits\n"
            "- configure selected feature attributes\n- add selected-feature"
        )
        assert gh_calls[0][gh_calls[0].index("--repo") : gh_calls[0].index("--json")] == [
            "--repo",
            "owner/repo",
            "--head",
            "selected-feature",
            "--state",
            "all",
            "--limit",
            "2",
        ]

    def test_rejects_existing_pr_with_unapproved_target(self, git_env: dict):
        """An incompatible existing PR blocks before its branch can be pushed."""
        worktree = create_managed_feature(git_env, "existing-pr-feature")
        write_managed_manifest(git_env["project"], [managed_record("existing-pr-feature")])
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                if args[1:3] == ["pr", "list"]:
                    return _managed_pr_result(
                        args,
                        base_sha="f" * 40,
                        head_sha=feature_head,
                    )
                raise AssertionError("managed publication must not create a mismatched PR")
            return real_run(args, **kwargs)

        with (
            patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature(
                "existing-pr-feature", [git_env["repo_ctx"]]
            )

        assert result["success"] is False
        assert len(gh_calls) == 1
        assert set(gh_calls[0][gh_calls[0].index("--json") + 1].split(",")) == {
            "url",
            "baseRefName",
            "baseRefOid",
            "headRefName",
            "headRefOid",
            "isCrossRepository",
            "state",
        }
        assert "existing-pr-feature" not in git(git_env["origin"], "branch").stdout

    def test_bounds_managed_pr_metadata_before_gh(self, git_env: dict):
        """Oversized commit subjects never become unbounded gh argv values."""
        worktree = create_managed_feature(git_env, "oversized-metadata-feature")
        git(worktree, "commit", "--allow-empty", "-m", "x" * 80)
        write_managed_manifest(git_env["project"], [managed_record("oversized-metadata-feature")])
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            _no_pr_result([]),
            _no_pr_result([]),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
            _managed_pr_result(
                [],
                base_sha=base_sha,
                head_sha=feature_head,
                branch_name="oversized-metadata-feature",
            ),
        ]
        with (
            patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync._MAX_MANAGED_PR_TITLE_BYTES", 16),
            patch("pynchy.host.git_ops.sync._MAX_MANAGED_PR_BODY_BYTES", 16),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature(
                "oversized-metadata-feature", [git_env["repo_ctx"]]
            )

        assert result["success"] is True
        create_call = gh_calls[2]
        assert create_call[create_call.index("--title") + 1] == (
            "Changes from managed feature oversized-metadata-feature"
        )
        assert create_call[create_call.index("--body") + 1].endswith(
            "Commit summaries omitted because they exceed host publication limits."
        )

    def test_rejects_closed_existing_pr(self, git_env: dict):
        """A closed same-branch PR cannot satisfy managed publication."""
        worktree = create_managed_feature(git_env, "closed-pr-feature")
        write_managed_manifest(git_env["project"], [managed_record("closed-pr-feature")])
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                if args[1:3] == ["pr", "list"]:
                    return _managed_pr_result(
                        args,
                        base_sha=base_sha,
                        head_sha=feature_head,
                        state="MERGED",
                    )
                raise AssertionError("managed publication must not create a closed PR")
            return real_run(args, **kwargs)

        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature("closed-pr-feature", [git_env["repo_ctx"]])

        assert result["success"] is False
        assert "existing PR is not open" in result["message"]
        assert len(gh_calls) == 1
        assert "closed-pr-feature" not in git(git_env["origin"], "branch").stdout

    def test_blocks_when_existing_pr_lookup_fails(self, git_env: dict):
        """A GitHub lookup failure never becomes permission to force-push."""
        create_managed_feature(git_env, "lookup-failure-feature")
        write_managed_manifest(git_env["project"], [managed_record("lookup-failure-feature")])
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr="offline",
                )
            return real_run(args, **kwargs)

        with (
            patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature(
                "lookup-failure-feature", [git_env["repo_ctx"]]
            )

        assert result == {
            "success": False,
            "message": "Publication blocked: could not inspect existing managed feature PR.",
        }
        assert len(gh_calls) == 1
        assert "lookup-failure-feature" not in git(git_env["origin"], "branch").stdout

    @pytest.mark.parametrize(
        ("head_ref_name", "cross_repository"),
        [("other-branch", False), (None, True)],
    )
    def test_rejects_nonlocal_or_wrong_branch_existing_pr(
        self,
        git_env: dict,
        head_ref_name: str | None,
        cross_repository: bool,
    ) -> None:
        """A matching OID cannot substitute for the selected local branch."""
        worktree = create_managed_feature(git_env, "bound-pr-feature")
        write_managed_manifest(git_env["project"], [managed_record("bound-pr-feature")])
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return _managed_pr_result(
                    args,
                    base_sha=base_sha,
                    head_sha=feature_head,
                    head_ref_name=head_ref_name,
                    cross_repository=cross_repository,
                )
            return real_run(args, **kwargs)

        with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run):
            result = host_create_pr_from_managed_feature("bound-pr-feature", [git_env["repo_ctx"]])

        assert result["success"] is False
        assert "existing PR is not open" in result["message"]
        assert "bound-pr-feature" not in git(git_env["origin"], "branch").stdout

    def test_updates_existing_open_pr_after_preflight(self, git_env: dict):
        """Preflight accepts an open PR before its inspected head gets pushed."""
        worktree = create_managed_feature(git_env, "existing-open-feature")
        write_managed_manifest(git_env["project"], [managed_record("existing-open-feature")])
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        git(
            git_env["project"],
            "push",
            str(git_env["origin"]),
            "main:existing-open-feature",
        )
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                if len(gh_calls) == 1:
                    return _managed_pr_result(args, base_sha=base_sha, head_sha=base_sha)
                return _managed_pr_result(args, base_sha=base_sha, head_sha=feature_head)
            return real_run(args, **kwargs)

        with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run):
            result = host_create_pr_from_managed_feature(
                "existing-open-feature", [git_env["repo_ctx"]]
            )

        assert result["success"] is True
        assert len(gh_calls) == 2
        assert (
            git(git_env["origin"], "rev-parse", "existing-open-feature").stdout.strip()
            == feature_head
        )

    def test_blocks_post_push_pr_head_race(self, git_env: dict):
        """A PR that appears with another head after push blocks publication."""
        worktree = create_managed_feature(git_env, "head-race-feature")
        write_managed_manifest(git_env["project"], [managed_record("head-race-feature")])
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                if len(gh_calls) == 1:
                    return _no_pr_result(args)
                git(
                    git_env["origin"],
                    "update-ref",
                    "refs/heads/head-race-feature",
                    base_sha,
                )
                return _managed_pr_result(args, base_sha=base_sha, head_sha=base_sha)
            return real_run(args, **kwargs)

        with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run):
            result = host_create_pr_from_managed_feature("head-race-feature", [git_env["repo_ctx"]])

        assert result["success"] is False
        assert "existing PR is not open" in result["message"]
        assert len(gh_calls) == 2
        assert git(git_env["origin"], "rev-parse", "head-race-feature").stdout.strip() == base_sha
        assert feature_head != base_sha

    def test_rejects_created_pr_with_mismatched_head(self, git_env: dict):
        """A newly created PR must retain the inspected head commit."""
        worktree = create_managed_feature(git_env, "created-pr-race-feature")
        write_managed_manifest(git_env["project"], [managed_record("created-pr-race-feature")])
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                gh_calls.append(args)
                if args[1:3] == ["pr", "list"] and len(gh_calls) < 3:
                    return _no_pr_result(args)
                if args[1:3] == ["pr", "create"]:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="https://github.com/owner/repo/pull/1\n",
                        stderr="",
                    )
                if args[1:3] == ["pr", "close"]:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="",
                        stderr="",
                    )
                return _managed_pr_result(args, base_sha=base_sha, head_sha=base_sha)
            return real_run(args, **kwargs)

        with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run):
            result = host_create_pr_from_managed_feature(
                "created-pr-race-feature", [git_env["repo_ctx"]]
            )

        assert result["success"] is False
        assert "existing PR is not open" in result["message"]
        assert [call[1:3] for call in gh_calls] == [
            ["pr", "list"],
            ["pr", "list"],
            ["pr", "create"],
            ["pr", "list"],
            ["pr", "close"],
        ]
        assert (
            git(git_env["origin"], "rev-parse", "created-pr-race-feature").stdout.strip()
            == feature_head
        )

    def test_rejects_remote_branch_race_before_pr_creation(self, git_env: dict):
        """Exact lease prevents changed remote branch from being overwritten."""
        worktree = create_managed_feature(git_env, "lease-feature")
        write_managed_manifest(git_env["project"], [managed_record("lease-feature")])
        feature_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
        main_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        git(git_env["project"], "push", str(git_env["origin"]), "lease-feature")
        real_run_git = run_git
        real_subprocess_run = subprocess.run
        raced = False

        def move_remote_before_push(*args, **kwargs):
            nonlocal raced
            if "push" in args and not raced:
                raced = True
                git(
                    git_env["origin"],
                    "update-ref",
                    "refs/heads/lease-feature",
                    main_sha,
                )
            return real_run_git(*args, **kwargs)

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return _no_pr_result(args)
            return real_subprocess_run(args, **kwargs)

        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync.run_git", side_effect=move_remote_before_push),
            patch(
                "pynchy.host.git_ops.sync.subprocess.run",
                side_effect=mock_run,
            ),
            patch("pynchy.host.git_ops.sync._open_or_update_pr") as open_pr,
        ):
            result = host_create_pr_from_managed_feature("lease-feature", [git_env["repo_ctx"]])

        assert result["success"] is False
        assert raced is True
        open_pr.assert_not_called()
        assert git(git_env["origin"], "rev-parse", "lease-feature").stdout.strip() == main_sha
        assert feature_sha != main_sha

    def test_preserves_sha256_object_format(self, tmp_path: Path):
        origin = make_bare_origin(tmp_path, object_format="sha256")
        project = make_project(tmp_path, origin)
        repo_ctx = RepoContext("owner/repo", project, tmp_path / "worktrees")
        git_env = {"origin": origin, "project": project, "repo_ctx": repo_ctx}
        worktree = create_managed_feature(git_env, "sha256-feature")
        write_managed_manifest(project, [managed_record("sha256-feature")])
        base_sha = git(origin, "rev-parse", "main").stdout.strip()
        feature_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            _no_pr_result([]),
            _no_pr_result([]),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
            _managed_pr_result(
                [], base_sha=base_sha, head_sha=feature_head, branch_name="sha256-feature"
            ),
        ]
        with (
            patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(origin),
            ),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature("sha256-feature", [repo_ctx])

        assert result["success"] is True
        assert (
            git(origin, "rev-parse", "sha256-feature").stdout.strip()
            == git(worktree, "rev-parse", "HEAD").stdout.strip()
        )

    def test_does_not_run_agent_gpg_configuration_for_pr_metadata(
        self, git_env: dict, tmp_path: Path
    ):
        worktree = create_managed_feature(git_env, "signed-feature")
        signed_head = replace_head_with_signed_commit(worktree)
        write_managed_manifest(git_env["project"], [managed_record("signed-feature")])
        marker = tmp_path / "gpg-ran"
        gpg_program = tmp_path / "hostile-gpg"
        gpg_program.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n",
            encoding="utf-8",
        )
        gpg_program.chmod(0o755)
        git(git_env["project"], "config", "log.showSignature", "true")
        git(git_env["project"], "config", "gpg.program", str(gpg_program))
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            _no_pr_result([]),
            _no_pr_result([]),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
            _managed_pr_result(
                [], base_sha=base_sha, head_sha=signed_head, branch_name="signed-feature"
            ),
        ]
        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature("signed-feature", [git_env["repo_ctx"]])

        assert result["success"] is True
        assert not marker.exists()
        assert git(git_env["origin"], "rev-parse", "signed-feature").stdout.strip() == signed_head

    def test_pushes_inspected_head_if_branch_advances_during_publication(self, git_env: dict):
        """Push source remains bound to commit Cop inspected."""
        worktree = create_managed_feature(git_env, "bound-feature")
        write_managed_manifest(git_env["project"], [managed_record("bound-feature")])
        inspected_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
        real_run_git = run_git
        advanced = False
        real_subprocess_run = subprocess.run

        def advance_then_push(*args, **kwargs):
            nonlocal advanced
            if "push" in args and not advanced:
                advanced = True
                (worktree / "after-inspection.txt").write_text("late commit\n", encoding="utf-8")
                git(worktree, "add", "after-inspection.txt")
                git(worktree, "commit", "-m", "advance after inspection")
            return real_run_git(*args, **kwargs)

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return mock_run.results.pop(0)
            return real_subprocess_run(args, **kwargs)

        mock_run.results = [
            _no_pr_result([]),
            _no_pr_result([]),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
            _managed_pr_result(
                [], base_sha=base_sha, head_sha=inspected_head, branch_name="bound-feature"
            ),
        ]
        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync.run_git", side_effect=advance_then_push),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_managed_feature(
                "bound-feature",
                [git_env["repo_ctx"]],
                expected_head_sha=inspected_head,
            )

        assert result["success"] is True
        assert advanced is True
        assert git(git_env["origin"], "rev-parse", "bound-feature").stdout.strip() == inspected_head
        assert git(worktree, "rev-parse", "HEAD").stdout.strip() != inspected_head
