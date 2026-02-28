"""Tests for repo-scoped token management.

Covers:
- get_repo_token() resolution chain
- ensure_repo_cloned() with token authentication
- _sanitize_token() credential scrubbing
- Container credential injection (scoped vs broad)
- git_env_with_token() environment building
- check_token_expiry() API header parsing
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import make_settings
from pydantic import SecretStr

from pynchy.config import RepoConfig, WorkspaceConfig
from pynchy.git_ops.repo import (
    RepoContext,
    _sanitize_token,
    check_token_expiry,
    get_repo_token,
)
from pynchy.git_ops.utils import git_env_with_token

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


REPO_SLUG = "owner/private-repo"
SCOPED_TOKEN = "github_pat_scoped_abc123"
BROAD_TOKEN = "ghp_broad_token_xyz"
GH_CLI_TOKEN = "gho_cli_token_789"


# ---------------------------------------------------------------------------
# get_repo_token() resolution chain
# ---------------------------------------------------------------------------


class TestGetRepoToken:
    def test_per_repo_token_wins(self):
        """Per-repo token takes highest priority."""
        s = make_settings(
            repos={REPO_SLUG: RepoConfig(token=SecretStr(SCOPED_TOKEN))},
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=GH_CLI_TOKEN),
        ):
            assert get_repo_token(REPO_SLUG) == SCOPED_TOKEN

    def test_broad_token_fallback(self):
        """Falls back to secrets.gh_token when no per-repo token."""
        s = make_settings(
            repos={REPO_SLUG: RepoConfig()},
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=GH_CLI_TOKEN),
        ):
            assert get_repo_token(REPO_SLUG) == BROAD_TOKEN

    def test_gh_cli_fallback(self):
        """Falls back to gh CLI when no config tokens."""
        s = make_settings(
            repos={REPO_SLUG: RepoConfig()},
            secrets=MagicMock(gh_token=None),
        )
        with (
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=GH_CLI_TOKEN),
        ):
            assert get_repo_token(REPO_SLUG) == GH_CLI_TOKEN

    def test_no_token_available(self):
        """Returns None when no token is available anywhere."""
        s = make_settings(
            repos={REPO_SLUG: RepoConfig()},
            secrets=MagicMock(gh_token=None),
        )
        with (
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=None),
        ):
            assert get_repo_token(REPO_SLUG) is None

    def test_unknown_slug_uses_fallback(self):
        """Slug not in repos config still gets fallback tokens."""
        s = make_settings(
            repos={},
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=None),
        ):
            assert get_repo_token("unknown/repo") == BROAD_TOKEN


# ---------------------------------------------------------------------------
# _sanitize_token
# ---------------------------------------------------------------------------


class TestSanitizeToken:
    def test_strips_token_from_text(self):
        text = (
            f"fatal: Authentication failed for 'https://x-access-token:{SCOPED_TOKEN}@github.com/'"
        )
        result = _sanitize_token(text, SCOPED_TOKEN)
        assert SCOPED_TOKEN not in result
        assert "***" in result

    def test_no_token_returns_original(self):
        text = "fatal: repository not found"
        assert _sanitize_token(text, None) == text

    def test_no_match_returns_original(self):
        text = "fatal: repository not found"
        assert _sanitize_token(text, SCOPED_TOKEN) == text


# ---------------------------------------------------------------------------
# ensure_repo_cloned
# ---------------------------------------------------------------------------


class TestEnsureRepoCloned:
    def test_existing_repo_returns_true(self, tmp_path: Path):
        """Existing repo directory short-circuits without cloning."""
        from pynchy.git_ops.repo import ensure_repo_cloned

        repo_ctx = RepoContext(slug=REPO_SLUG, root=tmp_path, worktrees_dir=tmp_path / "wt")
        assert ensure_repo_cloned(repo_ctx) is True

    def test_clone_with_token(self, tmp_path: Path):
        """Clones with token in URL, then resets remote URL."""
        from pynchy.git_ops.repo import ensure_repo_cloned

        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return _ok()

        with (
            patch("pynchy.git_ops.repo.get_repo_token", return_value=SCOPED_TOKEN),
            patch("subprocess.run", side_effect=mock_run),
        ):
            assert ensure_repo_cloned(repo_ctx) is True

        # First call: clone with token in URL
        clone_cmd = calls[0]
        assert "clone" in clone_cmd[1]
        assert f"x-access-token:{SCOPED_TOKEN}@github.com" in clone_cmd[2]

        # Second call: reset remote URL (no token)
        set_url_cmd = calls[1]
        assert "set-url" in set_url_cmd
        assert SCOPED_TOKEN not in str(set_url_cmd)

    def test_clone_without_token(self, tmp_path: Path):
        """Clones with bare URL when no token available."""
        from pynchy.git_ops.repo import ensure_repo_cloned

        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return _ok()

        with (
            patch("pynchy.git_ops.repo.get_repo_token", return_value=None),
            patch("subprocess.run", side_effect=mock_run),
        ):
            assert ensure_repo_cloned(repo_ctx) is True

        clone_cmd = calls[0]
        assert f"https://github.com/{REPO_SLUG}" in clone_cmd[2]
        assert "x-access-token" not in str(clone_cmd)

    def test_clone_failure_sanitizes_stderr(self, tmp_path: Path):
        """Failed clone logs sanitized stderr (no token leak)."""
        from pynchy.git_ops.repo import ensure_repo_cloned

        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        def mock_run(cmd, **kwargs):
            return _fail(
                f"fatal: could not read password for "
                f"'https://x-access-token:{SCOPED_TOKEN}@github.com'"
            )

        with (
            patch("pynchy.git_ops.repo.get_repo_token", return_value=SCOPED_TOKEN),
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        # Verify token was sanitized in the logged error
        error_call = mock_logger.error.call_args
        assert SCOPED_TOKEN not in str(error_call)


# ---------------------------------------------------------------------------
# Container credential injection
# ---------------------------------------------------------------------------


class TestContainerCredentialInjection:
    def test_admin_gets_broad_token(self, tmp_path: Path):
        """Admin container gets the broad gh_token."""
        from pynchy.host.container_manager.credentials import _write_env_file

        s = make_settings(
            data_dir=tmp_path,
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=(None, None),
            ),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="admin")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert BROAD_TOKEN in content
            assert "GH_TOKEN" in content

    def test_non_admin_with_repo_access_gets_scoped_token(self, tmp_path: Path):
        """Non-admin container with repo_access gets the repo-scoped token."""
        from pynchy.host.container_manager.credentials import _write_env_file

        s = make_settings(
            data_dir=tmp_path,
            repos={REPO_SLUG: RepoConfig(token=SecretStr(SCOPED_TOKEN))},
            workspaces={
                "code-improver": WorkspaceConfig(
                    name="Code Improver",
                    is_admin=False,
                    repo_access=REPO_SLUG,
                ),
            },
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=(None, None),
            ),
        ):
            env_dir = _write_env_file(is_admin=False, group_folder="code-improver")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            # Gets the scoped token, not the broad one
            assert SCOPED_TOKEN in content
            assert BROAD_TOKEN not in content

    def test_non_admin_without_repo_access_gets_no_token(self, tmp_path: Path):
        """Non-admin container without repo_access gets no GH_TOKEN."""
        from pynchy.host.container_manager.credentials import _write_env_file

        s = make_settings(
            data_dir=tmp_path,
            workspaces={
                "basic-group": WorkspaceConfig(
                    name="Basic",
                    is_admin=False,
                ),
            },
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=("Test", "test@test.com"),
            ),
        ):
            env_dir = _write_env_file(is_admin=False, group_folder="basic-group")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN" not in content
            assert BROAD_TOKEN not in content

    def test_non_admin_with_repo_access_no_token_configured(self, tmp_path: Path):
        """Non-admin with repo_access but no token configured gets no GH_TOKEN."""
        from pynchy.host.container_manager.credentials import _write_env_file

        s = make_settings(
            data_dir=tmp_path,
            repos={REPO_SLUG: RepoConfig()},  # no token
            workspaces={
                "code-improver": WorkspaceConfig(
                    name="Code Improver",
                    is_admin=False,
                    repo_access=REPO_SLUG,
                ),
            },
            secrets=MagicMock(gh_token=SecretStr(BROAD_TOKEN)),
        )
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=("Test", "test@test.com"),
            ),
        ):
            env_dir = _write_env_file(is_admin=False, group_folder="code-improver")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            # No token injected — repo_access without a scoped token
            assert "GH_TOKEN" not in content


# ---------------------------------------------------------------------------
# git_env_with_token
# ---------------------------------------------------------------------------


class TestGitEnvWithToken:
    def test_returns_none_without_token(self):
        """No token -> returns None (callers use ambient credentials)."""
        with patch("pynchy.git_ops.repo.get_repo_token", return_value=None):
            assert git_env_with_token(REPO_SLUG) is None

    def test_returns_env_with_credential_helper(self):
        """Token -> env dict includes GH_TOKEN and credential helper config."""
        with patch("pynchy.git_ops.repo.get_repo_token", return_value=SCOPED_TOKEN):
            env = git_env_with_token(REPO_SLUG)
            assert env is not None
            assert env["GH_TOKEN"] == SCOPED_TOKEN
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            assert env["GIT_CONFIG_COUNT"] == "2"
            assert "x-access-token" in env["GIT_CONFIG_VALUE_0"]
            assert SCOPED_TOKEN in env["GIT_CONFIG_VALUE_1"]


# ---------------------------------------------------------------------------
# check_token_expiry
# ---------------------------------------------------------------------------


class TestCheckTokenExpiry:
    def test_warns_on_near_expiry(self):
        """Logs warning when token expires within 30 days."""
        import datetime

        soon = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=15)
        expiry_str = soon.strftime("%Y-%m-%d %H:%M:%S UTC")
        headers = (
            "HTTP/2 200\n"
            f"github-authentication-token-expiration: {expiry_str}\n"
            '{"resources": {}}'
        )
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_TOKEN)
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
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_TOKEN)
            mock_logger.error.assert_called_once()
            assert "EXPIRED" in str(mock_logger.error.call_args)

    def test_ok_on_far_expiry(self):
        """No warning when token has plenty of time left."""
        import datetime

        far = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=200)
        expiry_str = far.strftime("%Y-%m-%d %H:%M:%S UTC")
        headers = (
            "HTTP/2 200\n"
            f"github-authentication-token-expiration: {expiry_str}\n"
            '{"resources": {}}'
        )
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_TOKEN)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()
            mock_logger.debug.assert_called_once()

    def test_silent_on_api_failure(self):
        """Silently continues if the API call fails."""
        with (
            patch("subprocess.run", return_value=_fail()),
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_TOKEN)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()

    def test_silent_on_no_expiry_header(self):
        """Silently continues if the response has no expiry header (classic token)."""
        headers = 'HTTP/2 200\n{"resources": {}}'
        with (
            patch("subprocess.run", return_value=_ok(headers)),
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_TOKEN)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()

    def test_silent_on_timeout(self):
        """Silently continues on subprocess timeout."""
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)),
            patch("pynchy.git_ops.repo.logger") as mock_logger,
        ):
            check_token_expiry(REPO_SLUG, SCOPED_TOKEN)
            mock_logger.warning.assert_not_called()
            mock_logger.error.assert_not_called()
