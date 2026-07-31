"""Public coverage for credential-free and token-scoped Git environments."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - tests model the fixed subprocess result from Git.
from unittest.mock import patch

from pynchy.host.git_ops.api import git_env_with_token, push_local_commits


def test_git_environment_can_skip_global_identity_discovery() -> None:
    with patch("pynchy.host.git_ops.repo.get_repo_token", return_value="gh-token"):
        environment = git_env_with_token("tokenized-repo", inherit_host_environment=False)

    assert environment is not None
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_AUTHOR_NAME" not in environment


def test_git_environment_handles_partial_global_identity_configuration() -> None:
    responses = [
        subprocess.CompletedProcess([], 0, stdout="Alice\n", stderr=""),
        subprocess.CompletedProcess([], 1, stdout="", stderr=""),
    ]

    with (
        patch("pynchy.host.git_ops._environment.subprocess.run", side_effect=responses),
        patch(
            "pynchy.host.git_ops.utils.run_git",
            return_value=subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
        ) as run_git,
    ):
        assert push_local_commits(main_branch="main", skip_fetch=True) is True

    environment = run_git.call_args.kwargs["env"]
    assert environment["GIT_AUTHOR_NAME"] == "Alice"
    assert environment["GIT_COMMITTER_NAME"] == "Alice"
    assert "GIT_AUTHOR_EMAIL" not in environment


def test_git_environment_ignores_identity_lookup_failures() -> None:
    with (
        patch(
            "pynchy.host.git_ops._environment.subprocess.run",
            side_effect=OSError("git unavailable"),
        ),
        patch(
            "pynchy.host.git_ops.utils.run_git",
            return_value=subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
        ) as run_git,
    ):
        assert push_local_commits(main_branch="main", skip_fetch=True) is True

    assert "GIT_AUTHOR_NAME" not in run_git.call_args.kwargs["env"]


def test_git_environment_returns_none_without_a_repository_token() -> None:
    with patch("pynchy.host.git_ops.repo.get_repo_token", return_value=None):
        assert git_env_with_token("missing-token") is None


def test_git_environment_scopes_a_token_without_inheriting_host_variables(monkeypatch) -> None:
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")

    with patch("pynchy.host.git_ops.repo.get_repo_token", return_value="gh-token"):
        environment = git_env_with_token("tokenized-repo", inherit_host_environment=False)

    assert environment is not None
    assert environment["GH_TOKEN"] == "gh-token"  # noqa: S105  # pragma: allowlist secret
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "UNRELATED_HOST_SECRET" not in environment
    config = {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(environment["GIT_CONFIG_COUNT"]))
    }
    assert not config["credential.helper"]


def test_git_environment_recovers_from_an_invalid_inherited_config_count(monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "not-an-integer")

    with patch("pynchy.host.git_ops.repo.get_repo_token", return_value="gh-token"):
        environment = git_env_with_token("tokenized-repo")

    assert environment is not None
    assert environment["GIT_CONFIG_COUNT"] == "5"
    assert environment["GH_TOKEN"] == "gh-token"  # noqa: S105  # pragma: allowlist secret
