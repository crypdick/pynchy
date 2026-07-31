"""Public managed-feature publication behavior for isolated Git failures."""

from __future__ import annotations

import subprocess  # noqa: S404 - test doubles model fixed Git and gh commands.
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import host_create_pr_from_managed_feature, run_git
from tests.git_policy_support import (
    create_managed_feature,
    git,
    managed_record,
    no_pr_result,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)


@pytest.mark.action("lifecycle.managed.feature.publish")
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            "target",
            "Publication blocked: managed feature target changed after Cop inspection.",
        ),
        (
            "init",
            "Publication blocked: could not initialize isolated Git transport: init failed",
        ),
        (
            "fetch",
            "Publication blocked: could not fetch managed feature target.",
        ),
        (
            "remote",
            (
                "Publication blocked: could not verify managed feature remote branch: "
                "remote unavailable"
            ),
        ),
        (
            "invalid-remote",
            "Publication blocked: managed feature remote branch returned invalid data.",
        ),
        (
            "ambiguous-remote",
            "Publication blocked: managed feature remote branch returned invalid data.",
        ),
        ("push", "Push failed: permission denied"),
    ],
)
def test_isolated_transport_failures_block_publication(
    git_env: dict, failure: str, message: str
) -> None:
    """Every isolated transport failure fails closed before PR creation."""
    feature = f"transport-{failure}"
    worktree = create_managed_feature(git_env, feature)
    write_managed_manifest(git_env["project"], [managed_record(feature)])
    base_sha = git(git_env["origin"], "rev-parse", "main").stdout.strip()
    head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
    real_run_git = run_git
    real_subprocess_run = subprocess.run
    remote_ref = f"refs/heads/{feature}"

    def fake_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        failure_result = None
        if failure == "init" and args[:2] == ("init", "--bare"):
            failure_result = subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="init failed"
            )
        elif failure == "target" and args[-1] == "refs/heads/main" and "ls-remote" in args:
            failure_result = subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f"{'f' * 40}\trefs/heads/main\n",
                stderr="",
            )
        elif failure == "fetch" and "fetch" in args:
            failure_result = subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="offline"
            )
        elif failure == "remote" and args[-1] == remote_ref and "ls-remote" in args:
            failure_result = subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="remote unavailable"
            )
        elif failure == "invalid-remote" and args[-1] == remote_ref and "ls-remote" in args:
            failure_result = subprocess.CompletedProcess(
                args=args, returncode=0, stdout=f"not-a-sha\t{remote_ref}\n", stderr=""
            )
        elif failure == "ambiguous-remote" and args[-1] == remote_ref and "ls-remote" in args:
            failure_result = subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f"{'a' * 40}\t{remote_ref}\n{'b' * 40}\t{remote_ref}\n",
                stderr="",
            )
        elif failure == "push" and "push" in args:
            failure_result = subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="permission denied"
            )
        if failure_result is not None:
            return failure_result
        return real_run_git(*args, **kwargs)

    def fake_subprocess(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            return no_pr_result(args)
        return real_subprocess_run(args, check=kwargs.pop("check", False), **kwargs)

    with (
        patch("pynchy.host.git_ops.sync.run_git", side_effect=fake_git),
        patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=fake_subprocess),
    ):
        result = host_create_pr_from_managed_feature(feature, [git_env["repo_ctx"]])

    assert result == {"success": False, "message": message}
    assert head_sha != base_sha
    assert feature not in git(git_env["origin"], "branch").stdout
