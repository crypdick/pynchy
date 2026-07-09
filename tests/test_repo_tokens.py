"""Tests for repo-scoped token management.

Covers:
- get_repo_token() resolution chain
- ensure_repo_cloned() with token authentication
- token scrubbing in clone-failure logs
- Container credential injection (scoped vs broad)
- git_env_with_token() environment building
- check_token_expiry() API header parsing
"""

from __future__ import annotations

import datetime
import subprocess  # noqa: S404, RUF100 - test helpers mock subprocess behavior and exceptions
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

from conftest import make_settings
from pydantic import SecretStr

from pynchy.config import WorkspaceConfig
from pynchy.config.models import RepoConfig, ReposConfig
from pynchy.host.container_manager import credentials
from pynchy.host.container_manager.onecli import OneCliMaterial
from pynchy.host.git_ops.repo import (
    RepoContext,
    check_token_expiry,
    ensure_repo_cloned,
    get_repo_context,
    get_repo_token,
    repo_container_path,
)
from pynchy.host.git_ops.utils import git_env_with_token
from pynchy.types import VolumeMount

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


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

        with patch("pynchy.config.get_settings", return_value=s):
            repo_ctx = get_repo_context("owner/project")

        assert repo_ctx == RepoContext(
            slug="owner/project",
            root=repos_root / "project",
            worktrees_dir=worktrees_root / "owner" / "project",
        )

    def test_repo_container_path_uses_workspace_repos_pattern(self):
        assert repo_container_path("owner/project") == "/workspace/repos/owner/project"


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
            patch("pynchy.config.get_settings", return_value=s),
            patch(
                "pynchy.host.container_manager.credentials._read_gh_token",
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
            patch("pynchy.config.get_settings", return_value=s),
            patch(
                "pynchy.host.container_manager.credentials._read_gh_token",
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
            patch("pynchy.config.get_settings", return_value=s),
            patch(
                "pynchy.host.container_manager.credentials._read_gh_token",
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
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=None),
        ):
            assert get_repo_token(REPO_SLUG) is None

    def test_unknown_slug_uses_fallback(self):
        """Slug not in repos config still gets fallback tokens."""
        s = make_settings(
            repos=_repos(),
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        with (
            patch("pynchy.config.get_settings", return_value=s),
            patch("pynchy.host.container_manager.credentials._read_gh_token", return_value=None),
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
            patch("subprocess.run", side_effect=lambda *a, **k: _fail(stderr)),
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
    def test_existing_repo_returns_true(self, tmp_path: Path):
        """Existing repo directory short-circuits without cloning."""
        repo_ctx = RepoContext(slug=REPO_SLUG, root=tmp_path, worktrees_dir=tmp_path / "wt")
        assert ensure_repo_cloned(repo_ctx) is True

    def test_clone_with_token(self, tmp_path: Path):
        """Clones with bare URL and env-based token auth, then resets remote URL."""
        repo_root = tmp_path / "repo"
        repo_ctx = RepoContext(slug=REPO_SLUG, root=repo_root, worktrees_dir=tmp_path / "wt")

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return _ok()

        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=SCOPED_CREDENTIAL),
            patch("subprocess.run", side_effect=mock_run),
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

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            return _ok()

        with (
            patch("pynchy.host.git_ops.repo.get_repo_token", return_value=None),
            patch("subprocess.run", side_effect=mock_run),
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
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.host.git_ops.repo.logger") as mock_logger,
        ):
            assert ensure_repo_cloned(repo_ctx) is False

        # Verify token was sanitized in the logged error
        error_call = mock_logger.error.call_args
        assert SCOPED_CREDENTIAL not in str(error_call)


# ---------------------------------------------------------------------------
# Container credential injection
# ---------------------------------------------------------------------------


class TestContainerCredentialInjection:
    def test_admin_gets_broad_token(self, tmp_path: Path):
        """Admin container gets the broad gh_token."""
        s = make_settings(
            data_dir=tmp_path,
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=(None, None),
            ),
        ):
            env_dir = credentials.write_env_file(is_admin=True, group_folder="admin")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert BROAD_CREDENTIAL in content
            assert "GH_TOKEN" in content

    def test_non_admin_with_repo_access_gets_scoped_token(self, tmp_path: Path):
        """Non-admin container with repo_access gets the repo-scoped token."""
        s = make_settings(
            data_dir=tmp_path,
            repos=_repos({REPO_SLUG: RepoConfig(token=SecretStr(SCOPED_CREDENTIAL))}),
            workspaces={"code-improver": WorkspaceConfig()},
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        fake_resolved = MagicMock(repo=[REPO_SLUG])
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=(None, None),
            ),
            patch(
                "pynchy.host.orchestrator.workspace_config.load_resolved_config",
                return_value=fake_resolved,
            ),
        ):
            env_dir = credentials.write_env_file(is_admin=False, group_folder="code-improver")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            # Gets the scoped token, not the broad one
            assert SCOPED_CREDENTIAL in content
            assert BROAD_CREDENTIAL not in content

    def test_non_admin_without_repo_access_gets_no_token(self, tmp_path: Path):
        """Non-admin container without repo_access gets no GH_TOKEN."""
        s = make_settings(
            data_dir=tmp_path,
            workspaces={
                "basic-group": WorkspaceConfig(),
            },
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        fake_resolved = MagicMock(repo=[])
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=("Test", "test@test.com"),
            ),
            patch(
                "pynchy.host.orchestrator.workspace_config.load_resolved_config",
                return_value=fake_resolved,
            ),
        ):
            env_dir = credentials.write_env_file(is_admin=False, group_folder="basic-group")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN" not in content
            assert BROAD_CREDENTIAL not in content

    def test_non_admin_with_repo_access_no_token_configured(self, tmp_path: Path):
        """Non-admin with repo_access but no token configured gets no GH_TOKEN."""
        s = make_settings(
            data_dir=tmp_path,
            repos=_repos({REPO_SLUG: RepoConfig()}),
            workspaces={"code-improver": WorkspaceConfig()},
            secrets=MagicMock(gh_token=SecretStr(BROAD_CREDENTIAL)),
        )
        fake_resolved = MagicMock(repo=[REPO_SLUG])
        with (
            patch("pynchy.host.container_manager.credentials.get_settings", return_value=s),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.container_manager.credentials._read_git_identity",
                return_value=("Test", "test@test.com"),
            ),
            patch(
                "pynchy.host.orchestrator.workspace_config.load_resolved_config",
                return_value=fake_resolved,
            ),
        ):
            env_dir = credentials.write_env_file(is_admin=False, group_folder="code-improver")
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
        with patch("pynchy.host.git_ops.repo.get_repo_token", return_value=None):
            assert git_env_with_token(REPO_SLUG) is None

    def test_returns_env_with_credential_helper(self):
        """Token -> env dict includes GH_TOKEN and credential helper config."""
        with patch("pynchy.host.git_ops.repo.get_repo_token", return_value=SCOPED_CREDENTIAL):
            env = git_env_with_token(REPO_SLUG)
            assert env is not None
            assert env["GH_TOKEN"] == SCOPED_CREDENTIAL
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            assert env["GIT_CONFIG_COUNT"] == "2"
            assert "x-access-token" in env["GIT_CONFIG_VALUE_0"]
            assert SCOPED_CREDENTIAL in env["GIT_CONFIG_VALUE_1"]

    def test_onecli_enabled_uses_proxy_env_without_raw_token(self, tmp_path: Path):
        """OneCLI enabled -> host git uses proxy/CA env and never resolves raw tokens."""
        ca_host_path = tmp_path / "onecli-ca.pem"
        ca_container_path = str(PurePosixPath("/", "tmp", "onecli-ca.pem"))
        material = OneCliMaterial(
            env_vars={
                "HTTPS_PROXY": "http://onecli-proxy",
                "SSL_CERT_FILE": ca_container_path,
            },
            mounts=[
                VolumeMount(
                    host_path=str(ca_host_path),
                    container_path=ca_container_path,
                    readonly=True,
                )
            ],
            warnings=[],
        )
        s = make_settings()
        s.onecli.enabled = True

        with (
            patch("pynchy.host.git_ops.utils.get_settings", return_value=s),
            patch(
                "pynchy.host.git_ops.utils.prepare_onecli_material",
                return_value=material,
                create=True,
            ),
            patch("pynchy.host.git_ops.repo.get_repo_token") as get_token,
        ):
            env = git_env_with_token(REPO_SLUG, group_folder="code-improver")

        assert env is not None
        assert env["HTTPS_PROXY"] == "http://onecli-proxy"
        assert env["SSL_CERT_FILE"] == str(ca_host_path)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GH_TOKEN" not in env
        assert "GIT_CONFIG_VALUE_1" not in env
        get_token.assert_not_called()


# ---------------------------------------------------------------------------
# check_token_expiry
# ---------------------------------------------------------------------------


class TestCheckTokenExpiry:
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
