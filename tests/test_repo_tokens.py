"""Tests for repo-scoped token management.

Covers:
- get_repo_token() resolution chain
- ensure_repo_cloned() with token authentication
- token scrubbing in clone-failure logs
- git_env_with_token() environment building
- check_token_expiry() API header parsing
"""

from __future__ import annotations

import datetime
import os
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings
from pydantic import SecretStr

from pynchy.config.api import RepoConfig, ReposConfig
from pynchy.host.git_ops.api import (
    RepoContext,
    check_token_expiry,
    ensure_repo_cloned,
    get_repo_context,
    get_repo_token,
    git_env_with_token,
    repo_container_path,
    run_git,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


def _init_ready_repo(path: Path, *, origin: str | None = None) -> None:
    path.mkdir(parents=True)
    assert run_git("init", "-b", "main", cwd=path).returncode == 0
    assert run_git("config", "user.email", "tests@example.invalid", cwd=path).returncode == 0
    assert run_git("config", "user.name", "Pynchy Tests", cwd=path).returncode == 0
    (path / "README.md").write_text("ready\n")
    assert run_git("add", "README.md", cwd=path).returncode == 0
    assert run_git("commit", "-m", "initial", cwd=path).returncode == 0
    assert (
        run_git(
            "remote",
            "add",
            "origin",
            origin or f"https://github.com/{REPO_SLUG}",
            cwd=path,
        ).returncode
        == 0
    )


def _successful_clone_mock(calls: list[list[str]]):
    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "clone":
            Path(cmd[3]).mkdir(parents=True)
            return _ok()
        if cmd[1:3] == ["rev-parse", "--show-toplevel"]:
            return _ok(f"{kwargs['cwd']}\n")
        if cmd[1:3] == ["rev-parse", "--verify"]:
            return _ok("abc123\n")
        if cmd[1:4] == ["remote", "get-url", "origin"]:
            return _ok(f"https://github.com/{REPO_SLUG}\n")
        return _ok()

    return mock_run


REPO_SLUG = "owner/private-repo"
SCOPED_CREDENTIAL = "scoped-credential-abc123"
BROAD_CREDENTIAL = "broad-credential-xyz"
GH_CLI_CREDENTIAL = "cli-credential-789"


def _repos(overrides: dict[str, RepoConfig] | None = None) -> ReposConfig:
    return ReposConfig(overrides=overrides or {})


class TestRepoContext:
    def test_repo_slug_without_override_resolves_under_default_root(self, tmp_path: Path):
        repos_root = tmp_path / "repos"
        worktrees_root = tmp_path / "worktrees"
        s = make_settings(repos=ReposConfig(root=repos_root), worktrees_dir=worktrees_root)

        with patch("pynchy.config.api.get_settings", return_value=s):
            repo_ctx = get_repo_context("owner/project")

        assert repo_ctx == RepoContext(
            slug="owner/project",
            root=repos_root / "project",
            worktrees_dir=worktrees_root / "owner" / "project",
        )

    def test_repo_container_path_uses_workspace_repos_pattern(self):
        assert repo_container_path("owner/project") == "/home/agent/src/owner/project"


# ---------------------------------------------------------------------------
# get_repo_token() resolution chain
# ---------------------------------------------------------------------------


class TestGetRepoToken:
    def test_per_repo_token_wins(self):
        """Per-repo token takes highest priority."""
        s = make_settings(
            repos=_repos({REPO_SLUG: RepoConfig(token=SecretStr(SCOPED_CREDENTIAL))}),
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        with (
            patch("pynchy.config.api.get_settings", return_value=s),
            patch(
                "pynchy.host.git_ops.repo._read_gh_token",
                return_value=GH_CLI_CREDENTIAL,
            ),
        ):
            assert get_repo_token(REPO_SLUG) == SCOPED_CREDENTIAL

    def test_broad_token_fallback(self):
        """Falls back to secrets.gh_token when no per-repo token."""
        s = make_settings(
            repos=_repos({REPO_SLUG: RepoConfig()}),
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        with (
            patch("pynchy.config.api.get_settings", return_value=s),
            patch(
                "pynchy.host.git_ops.repo._read_gh_token",
                return_value=GH_CLI_CREDENTIAL,
            ),
        ):
            assert get_repo_token(REPO_SLUG) == BROAD_CREDENTIAL

    def test_gh_cli_fallback(self):
        """Falls back to gh CLI when no config tokens."""
        s = make_settings(
            repos=_repos({REPO_SLUG: RepoConfig()}),
            secrets=MagicMock(gh_token=None),
        )
        with (
            patch("pynchy.config.api.get_settings", return_value=s),
            patch(
                "pynchy.host.git_ops.repo._read_gh_token",
                return_value=GH_CLI_CREDENTIAL,
            ),
        ):
            assert get_repo_token(REPO_SLUG) == GH_CLI_CREDENTIAL

    def test_no_token_available(self):
        """Returns None when no token is available anywhere."""
        s = make_settings(
            repos=_repos({REPO_SLUG: RepoConfig()}),
            secrets=MagicMock(gh_token=None),
        )
        with (
            patch("pynchy.config.api.get_settings", return_value=s),
            patch("pynchy.host.git_ops.repo._read_gh_token", return_value=None),
        ):
            assert get_repo_token(REPO_SLUG) is None

    def test_unknown_slug_uses_fallback(self):
        """Slug not in repos config still gets fallback tokens."""
        s = make_settings(
            repos=_repos(),
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        with (
            patch("pynchy.config.api.get_settings", return_value=s),
            patch("pynchy.host.git_ops.repo._read_gh_token", return_value=None),
        ):
            assert get_repo_token("unknown/repo") == BROAD_CREDENTIAL


# ---------------------------------------------------------------------------
# Token scrubbing in clone-failure logs (observed via ensure_repo_cloned)
# ---------------------------------------------------------------------------


class TestTokenScrubbingInLogs:
    """A failed clone must never leak the token into the logged stderr."""

    def _run_failing_clone(self, tmp_path: Path, token: str | None, stderr: str):
        repo_ctx = RepoContext(
            slug=REPO_SLUG, root=tmp_path / "repo", worktrees_dir=tmp_path / "wt"
        )
        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=token),
            patch(
                "pynchy.host.git_ops.utils._run_git_process",
                side_effect=lambda *a, **k: _fail(stderr),
            ),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            assert ensure_repo_cloned(repo_ctx) is False
        return str(mock_logger.error.call_args)

    def test_token_scrubbed_from_error_log(self, tmp_path: Path):
        stderr = f"fatal: Authentication failed for 'https://x-access-token:{SCOPED_CREDENTIAL}@github.com/'"
        logged = self._run_failing_clone(tmp_path, SCOPED_CREDENTIAL, stderr)
        assert SCOPED_CREDENTIAL not in logged
        assert "***" in logged

    def test_no_token_logs_stderr_verbatim(self, tmp_path: Path):
        logged = self._run_failing_clone(tmp_path, None, "fatal: repository not found")
        assert "repository not found" in logged

    def test_stderr_without_token_left_intact(self, tmp_path: Path):
        logged = self._run_failing_clone(tmp_path, SCOPED_CREDENTIAL, "fatal: repository not found")
        assert "repository not found" in logged
        assert "***" not in logged


# ---------------------------------------------------------------------------
# ensure_repo_cloned
# ---------------------------------------------------------------------------


class TestEnsureRepoCloned:
    @pytest.mark.parametrize(
        "origin",
        [
            f"https://github.com/{REPO_SLUG}.git",
            f"git@github.com:{REPO_SLUG}.git",
            f"ssh://git@github.com/{REPO_SLUG}.git",
        ],
    )
    def test_existing_repo_with_matching_github_origin_returns_true(
        self, tmp_path: Path, origin: str
    ):
        """A repository with HEAD and origin short-circuits without cloning."""
        repo_root = tmp_path / "repo"
        _init_ready_repo(repo_root, origin=origin)
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        with patch("pynchy.host.git_ops.repo._clone_repo_to") as clone:
            assert ensure_repo_cloned(repo_ctx) is True

        clone.assert_not_called()

    def test_clone_with_token(self, tmp_path: Path):
        """Clones with bare URL and env-based token auth, then resets remote URL."""
        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        calls = []

        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=SCOPED_CREDENTIAL),
            patch(
                "pynchy.host.git_ops.utils._run_git_process",
                side_effect=_successful_clone_mock(calls),
            ),
        ):
            assert ensure_repo_cloned(repo_ctx) is True

        # First call: clone bare URL, authenticated by env so argv never contains tokens.
        clone_cmd = calls[0]
        assert "clone" in clone_cmd[1]
        assert clone_cmd[2] == f"https://github.com/{REPO_SLUG}"
        assert SCOPED_CREDENTIAL not in str(clone_cmd)

        # Second call: reset remote URL (no token)
        set_url_cmd = calls[1]
        assert "set-url" in set_url_cmd
        assert SCOPED_CREDENTIAL not in str(set_url_cmd)

    def test_clone_without_token(self, tmp_path: Path):
        """Clones with bare URL when no token available."""
        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        calls = []

        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=None),
            patch(
                "pynchy.host.git_ops.utils._run_git_process",
                side_effect=_successful_clone_mock(calls),
            ),
        ):
            assert ensure_repo_cloned(repo_ctx) is True

        clone_cmd = calls[0]
        assert f"https://github.com/{REPO_SLUG}" in clone_cmd[2]
        assert "x-access-token" not in str(clone_cmd)

    def test_clone_failure_sanitizes_stderr(self, tmp_path: Path):
        """Failed clone logs sanitized stderr (no token leak)."""
        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        def mock_run(cmd, **kwargs):
            return _fail(
                f"fatal: could not read password for "
                f"'https://x-access-token:{SCOPED_CREDENTIAL}@github.com'"
            )

        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=SCOPED_CREDENTIAL),
            patch("pynchy.host.git_ops.utils._run_git_process", side_effect=mock_run),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        # Verify token was sanitized in the logged error
        error_call = mock_logger.error.call_args
        assert SCOPED_CREDENTIAL not in str(error_call)

    def test_clone_timeout_returns_false_without_blocking_startup(self, tmp_path: Path):
        repo_ctx = RepoContext(
            slug=REPO_SLUG, root=tmp_path / "repo", worktrees_dir=tmp_path / "wt"
        )

        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=None),
            patch(
                "pynchy.host.git_ops.utils._run_git_process",
                return_value=subprocess.CompletedProcess(
                    args=["git", "clone"],
                    returncode=124,
                    stdout="",
                    stderr="git command timed out after 30 seconds",
                ),
            ),
        ):
            assert ensure_repo_cloned(repo_ctx) is False

    def test_invalid_auto_managed_checkout_is_preserved_after_verified_recovery(
        self, tmp_path: Path
    ):
        repos_root = tmp_path / "repos"
        repo_root = repos_root / "private-repo"
        repo_root.mkdir(parents=True)
        (repo_root / "user-data.txt").write_text("preserve me\n")
        repo_ctx = RepoContext(
            slug=REPO_SLUG,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        settings = make_settings(repos=ReposConfig(root=repos_root))

        def clone_ready(_repo_ctx: RepoContext, target: Path) -> bool:
            _init_ready_repo(target)
            return True

        with (
            patch("pynchy.config.api.get_settings", return_value=settings),
            patch("pynchy.host.git_ops.repo._clone_repo_to", side_effect=clone_ready),
        ):
            assert ensure_repo_cloned(repo_ctx) is True

        recoveries = list(repos_root.glob(".private-repo.pynchy-recovery-*"))
        assert len(recoveries) == 1
        assert (recoveries[0] / "user-data.txt").read_text() == "preserve me\n"
        assert (repo_root / "README.md").read_text() == "ready\n"

    def test_wrong_origin_auto_managed_checkout_is_recovered(self, tmp_path: Path):
        repos_root = tmp_path / "repos"
        repo_root = repos_root / "private-repo"
        _init_ready_repo(
            repo_root,
            origin="https://github.com/other-owner/different-repo.git",
        )
        marker = repo_root / "wrong-repository.txt"
        marker.write_text("preserve me\n")
        repo_ctx = RepoContext(
            slug=REPO_SLUG,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        settings = make_settings(repos=ReposConfig(root=repos_root))

        def clone_ready(_repo_ctx: RepoContext, target: Path) -> bool:
            _init_ready_repo(target)
            return True

        with (
            patch("pynchy.config.api.get_settings", return_value=settings),
            patch("pynchy.host.git_ops.repo._clone_repo_to", side_effect=clone_ready),
        ):
            assert ensure_repo_cloned(repo_ctx) is True

        recoveries = list(repos_root.glob(".private-repo.pynchy-recovery-*"))
        assert len(recoveries) == 1
        assert (recoveries[0] / marker.name).read_text() == "preserve me\n"
        assert not (repo_root / marker.name).exists()

    def test_failed_recovery_leaves_invalid_checkout_untouched(self, tmp_path: Path):
        repos_root = tmp_path / "repos"
        repo_root = repos_root / "private-repo"
        repo_root.mkdir(parents=True)
        original = repo_root / "user-data.txt"
        original.write_text("preserve me\n")
        repo_ctx = RepoContext(
            slug=REPO_SLUG,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        settings = make_settings(repos=ReposConfig(root=repos_root))

        with (
            patch("pynchy.config.api.get_settings", return_value=settings),
            patch("pynchy.host.git_ops.repo._clone_repo_to", return_value=False),
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        assert original.read_text() == "preserve me\n"
        assert not list(repos_root.glob(".private-repo.pynchy-recovery-*"))

    def test_invalid_explicit_checkout_is_never_replaced(self, tmp_path: Path):
        repo_root = tmp_path / "operator-owned"
        repo_root.mkdir()
        original = repo_root / "user-data.txt"
        original.write_text("operator owned\n")
        repo_ctx = RepoContext(
            slug=REPO_SLUG,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        settings = make_settings(
            repos=ReposConfig(
                root=tmp_path / "repos",
                overrides={REPO_SLUG: RepoConfig(path=str(repo_root))},
            )
        )

        with (
            patch("pynchy.config.api.get_settings", return_value=settings),
            patch("pynchy.host.git_ops.repo._clone_repo_to") as clone,
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        clone.assert_not_called()
        assert original.read_text() == "operator owned\n"

    @pytest.mark.parametrize(
        ("unsafe_origin", "expected_error"),
        [
            (
                f"https://x-access-token:{SCOPED_CREDENTIAL}@github.com/{REPO_SLUG}.git",
                "embeds credentials",
            ),
            (
                f"ssh://{SCOPED_CREDENTIAL}@github.com/{REPO_SLUG}.git",
                "unsupported SSH userinfo",
            ),
            ("https://[invalid", "supported GitHub HTTPS or SSH URL"),
        ],
    )
    def test_unsafe_origin_is_rejected_without_mutating_explicit_checkout(
        self,
        tmp_path: Path,
        unsafe_origin: str,
        expected_error: str,
    ):
        repo_root = tmp_path / "operator-owned"
        _init_ready_repo(repo_root, origin=unsafe_origin)
        marker = repo_root / "operator-data.txt"
        marker.write_text("operator owned\n")
        repo_ctx = RepoContext(
            slug=REPO_SLUG,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        settings = make_settings(
            repos=ReposConfig(
                root=tmp_path / "repos",
                overrides={REPO_SLUG: RepoConfig(path=str(repo_root))},
            )
        )

        with (
            patch("pynchy.config.api.get_settings", return_value=settings),
            patch("pynchy.host.git_ops.repo._clone_repo_to") as clone,
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        clone.assert_not_called()
        assert marker.read_text() == "operator owned\n"
        assert SCOPED_CREDENTIAL not in str(mock_logger.error.call_args)
        assert expected_error in str(mock_logger.error.call_args)

    def test_nested_directory_is_not_mistaken_for_a_repo_checkout(self, tmp_path: Path):
        parent_repo = tmp_path / "parent"
        _init_ready_repo(parent_repo)
        repo_root = parent_repo / "partial-checkout"
        repo_root.mkdir()
        repo_ctx = RepoContext(
            slug=REPO_SLUG,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        settings = make_settings(
            repos=ReposConfig(
                root=tmp_path / "repos",
                overrides={REPO_SLUG: RepoConfig(path=str(repo_root))},
            )
        )

        with (
            patch("pynchy.config.api.get_settings", return_value=settings),
            patch("pynchy.host.git_ops.repo._clone_repo_to") as clone,
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        clone.assert_not_called()


# ---------------------------------------------------------------------------
# git_env_with_token
# ---------------------------------------------------------------------------


class TestGitEnvWithToken:
    def test_returns_none_without_token(self):
        """No token -> returns None (callers use ambient credentials)."""
        with patch("pynchy.host.git_ops.repo.get_repo_token", return_value=None):
            assert git_env_with_token(REPO_SLUG) is None

    def test_returns_env_with_credential_helper(self):
        """Token -> env dict includes GH_TOKEN and credential helper config."""
        with patch("pynchy.host.git_ops.repo.get_repo_token", return_value=SCOPED_CREDENTIAL):
            env = git_env_with_token(REPO_SLUG)
            assert env is not None
            assert env["GH_TOKEN"] == SCOPED_CREDENTIAL
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            start = int(env["GIT_CONFIG_COUNT"]) - 5
            assert not env[f"GIT_CONFIG_VALUE_{start}"]
            assert "x-access-token" in env[f"GIT_CONFIG_VALUE_{start + 1}"]
            assert SCOPED_CREDENTIAL in env[f"GIT_CONFIG_VALUE_{start + 2}"]
            assert env[f"GIT_CONFIG_VALUE_{start + 3}"] == "git@github.com:"
            assert env[f"GIT_CONFIG_VALUE_{start + 4}"] == "ssh://git@github.com/"

    def test_isolated_token_environment_skips_host_identity_lookup(self):
        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=SCOPED_CREDENTIAL),
            patch("pynchy.host.git_ops._environment.subprocess.run") as config,
        ):
            env = git_env_with_token(REPO_SLUG, inherit_host_environment=False)

        assert env is not None
        config.assert_not_called()
        assert env["GH_TOKEN"] == SCOPED_CREDENTIAL
        assert "GIT_AUTHOR_NAME" not in env
        assert "GIT_COMMITTER_NAME" not in env
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"


# ---------------------------------------------------------------------------
# check_token_expiry
# ---------------------------------------------------------------------------


class TestCheckTokenExpiry:
    def test_keeps_token_out_of_process_arguments(self):
        with patch("subprocess.run", return_value=_fail()) as run:
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)

        assert SCOPED_CREDENTIAL not in " ".join(run.call_args.args[0])
        assert run.call_args.kwargs["env"]["GH_TOKEN"] == SCOPED_CREDENTIAL

    def test_warns_on_near_expiry(self):
        """Logs warning when token expires within 30 days."""
        soon = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=15)
        expiry_str = soon.strftime("%Y-%m-%d %H:%M:%S UTC")
        headers = (
            "HTTP/2 200\n"
            f"github-authentication-token-expiration: {expiry_str}\n"
            '{"resources": {}}'
        )
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)
            mock_logger.warning.assert_called_once()
            assert "expiring soon" in str(mock_logger.warning.call_args)

    def test_errors_on_expired_token(self):
        """Logs error when token is already expired."""
        headers = (
            "HTTP/2 200\n"
            "github-authentication-token-expiration: 2024-01-01 00:00:00 UTC\n"
            '{"resources": {}}'
        )
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)
            mock_logger.error.assert_called_once()
            assert "EXPIRED" in str(mock_logger.error.call_args)

    def test_ok_on_far_expiry(self):
        """No warning when token has plenty of time left."""
        far = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=200)
        expiry_str = far.strftime("%Y-%m-%d %H:%M:%S UTC")
        headers = (
            "HTTP/2 200\n"
            f"github-authentication-token-expiration: {expiry_str}\n"
            '{"resources": {}}'
        )
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()
            mock_logger.debug.assert_called_once()

    def test_silent_on_api_failure(self):
        """Silently continues if the API call fails."""
        with (
            patch("subprocess.run", return_value=_fail()),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()

    def test_silent_on_no_expiry_header(self):
        """Silently continues if the response has no expiry header (classic token)."""
        headers = 'HTTP/2 200\n{"resources": {}}'
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()

    def test_silent_on_timeout(self):
        """Silently continues on subprocess timeout."""
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_CREDENTIAL)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()
