"""Tests for git_utils.py — shared git helpers.

These functions are used by worktree and git_sync modules.
They handle critical operations like pushing commits and detecting repo state,
with retry logic and error recovery that warrant thorough testing.
"""

from __future__ import annotations

import os
import signal
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from pynchy.host.git_ops.api import (
    count_commits,
    count_unpushed_commits,
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    is_repo_dirty,
    push_local_commits,
    run_git,
)


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Helper: simulate a successful git command."""
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Helper: simulate a failed git command."""
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# run_git
# ---------------------------------------------------------------------------


class TestRunGit:
    def test_uses_bounded_noninteractive_ssh_defaults(self):
        process = MagicMock(spec=subprocess.Popen)
        process.communicate.return_value = ("ok\n", "")
        process.returncode = 0
        with patch("subprocess.Popen", return_value=process) as mock_popen:
            result = run_git("ls-remote", "origin", cwd=Path("/repo"))

        assert result.returncode == 0
        assert result.stdout == "ok\n"
        kwargs = mock_popen.call_args.kwargs
        assert kwargs["start_new_session"] is True
        process.communicate.assert_called_once_with(timeout=30)
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "BatchMode=yes" in kwargs["env"]["GIT_SSH_COMMAND"]
        assert "ConnectTimeout=10" in kwargs["env"]["GIT_SSH_COMMAND"]
        assert "ConnectionAttempts=1" in kwargs["env"]["GIT_SSH_COMMAND"]

    def test_returns_failure_result_when_git_times_out(self):
        process = MagicMock(spec=subprocess.Popen)
        process.pid = 123
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="git fetch origin", timeout=30),
            ("", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=process),
            patch("os.killpg") as killpg,
        ):
            result = run_git("fetch", "origin", cwd=Path("/repo"))

        assert result.returncode == 124
        assert not result.stdout
        assert result.stderr == "git command timed out after 30 seconds"
        killpg.assert_called_once_with(123, signal.SIGTERM)

    def test_force_kills_process_group_when_graceful_termination_times_out(self):
        process = MagicMock(spec=subprocess.Popen)
        process.pid = 123
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="git fetch origin", timeout=30),
            subprocess.TimeoutExpired(cmd="git fetch origin", timeout=2),
            ("", ""),
        ]
        with (
            patch("subprocess.Popen", return_value=process),
            patch("os.killpg") as killpg,
        ):
            result = run_git("fetch", "origin", cwd=Path("/repo"))

        assert result.returncode == 124
        assert killpg.call_args_list == [
            call(123, signal.SIGTERM),
            call(123, signal.SIGKILL),
        ]

    def test_timeout_signals_descendant_processes(self, tmp_path: Path):
        ready_path = tmp_path / "child-ready"
        signal_path = tmp_path / "child-signal"
        fake_git = tmp_path / "git"
        fake_git.write_text(
            f"""#!{sys.executable}
import os
import signal
import subprocess
import sys
import time

child_code = '''
import os
import signal
import time

def handle_term(_signum, _frame):
    with open(os.environ["CHILD_SIGNAL_PATH"], "w") as marker:
        marker.write("SIGTERM")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_term)
with open(os.environ["CHILD_READY_PATH"], "w") as marker:
    marker.write("ready")
time.sleep(60)
'''
subprocess.Popen([sys.executable, "-c", child_code], env=os.environ.copy())
signal.signal(signal.SIGTERM, signal.SIG_IGN)
deadline = time.monotonic() + 0.5
while not os.path.exists(os.environ["CHILD_READY_PATH"]) and time.monotonic() < deadline:
    time.sleep(0.01)
while not os.path.exists(os.environ["CHILD_SIGNAL_PATH"]):
    time.sleep(0.01)
"""
        )
        fake_git.chmod(0o755)
        env = {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "CHILD_READY_PATH": str(ready_path),
            "CHILD_SIGNAL_PATH": str(signal_path),
        }

        result = run_git("fetch", cwd=tmp_path, timeout=1, env=env)

        assert result.returncode == 124
        assert ready_path.read_text() == "ready"
        assert signal_path.read_text() == "SIGTERM"


# ---------------------------------------------------------------------------
# detect_main_branch
# ---------------------------------------------------------------------------


class TestDetectMainBranch:
    def test_parses_branch_from_symbolic_ref(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git", return_value=_ok("refs/remotes/origin/main\n")
        ):
            assert detect_main_branch() == "main"

    def test_parses_non_standard_branch_name(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git", return_value=_ok("refs/remotes/origin/master\n")
        ):
            assert detect_main_branch() == "master"

    def test_preserves_slash_in_origin_default_branch(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git",
            return_value=_ok("refs/remotes/origin/release/main\n"),
        ):
            assert detect_main_branch() == "release/main"

    def test_falls_back_to_main_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert detect_main_branch() == "main"


# ---------------------------------------------------------------------------
# get_head_sha
# ---------------------------------------------------------------------------


class TestGetHeadSha:
    def test_returns_sha_on_success(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("abc123\n")):
            assert get_head_sha() == "abc123"

    def test_returns_unknown_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert get_head_sha() == "unknown"

    def test_returns_unknown_on_exception(self):
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError("no git")):
            assert get_head_sha() == "unknown"


# ---------------------------------------------------------------------------
# is_repo_dirty
# ---------------------------------------------------------------------------


class TestIsRepoDirty:
    def test_dirty_when_porcelain_has_output(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok(" M file.py\n")):
            assert is_repo_dirty() is True

    def test_clean_when_porcelain_is_empty(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("")):
            assert is_repo_dirty() is False

    def test_clean_when_whitespace_only(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("  \n")):
            assert is_repo_dirty() is False

    def test_returns_false_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert is_repo_dirty() is False

    def test_returns_false_on_exception(self):
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError):
            assert is_repo_dirty() is False


# ---------------------------------------------------------------------------
# count_unpushed_commits
# ---------------------------------------------------------------------------


class TestCountUnpushedCommits:
    def test_returns_count_on_success(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok("3\n"),  # rev-list --count
            ]
            assert count_unpushed_commits() == 3

    def test_returns_zero_when_nothing_to_push(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),
                _ok("0\n"),
            ]
            assert count_unpushed_commits() == 0

    def test_returns_zero_on_rev_list_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),
                _fail(),
            ]
            assert count_unpushed_commits() == 0

    def test_returns_zero_on_os_error(self):
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError):
            assert count_unpushed_commits() == 0

    def test_returns_zero_on_subprocess_timeout(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            assert count_unpushed_commits() == 0

    def test_handles_empty_stdout(self):
        """Empty rev-list output should be treated as 0 via the `or '0'` guard."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),
                _ok(""),
            ]
            assert count_unpushed_commits() == 0


# ---------------------------------------------------------------------------
# files_changed_between
# ---------------------------------------------------------------------------


class TestFilesChangedBetween:
    def test_returns_true_when_files_changed(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("src/app.py\n")):
            assert files_changed_between("aaa", "bbb", "src/") is True

    def test_returns_false_when_no_files_changed(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("")):
            assert files_changed_between("aaa", "bbb", "src/") is False

    def test_returns_false_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert files_changed_between("aaa", "bbb", "src/") is False


# ---------------------------------------------------------------------------
# push_local_commits — the most complex function with retry logic
# ---------------------------------------------------------------------------


class TestPushLocalCommits:
    """Tests for push_local_commits, which has a two-attempt rebase+push
    strategy to handle the race where origin advances between fetch and rebase.
    """

    def test_nothing_to_push_returns_true(self):
        """When rev-list shows 0 commits ahead, nothing to do."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("0\n"),  # rev-list --count
            ]
            assert push_local_commits() is True

    def test_rev_list_failure_returns_false(self):
        """A failed ahead check cannot prove the repository is in sync."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _fail(),  # rev-list fails
            ]
            assert push_local_commits() is False

    def test_successful_rebase_and_push(self):
        """Happy path: fetch, rebase, push all succeed."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("2\n"),  # rev-list: 2 commits ahead
                _ok(),  # rebase succeeds
                _ok(),  # push succeeds
            ]
            assert push_local_commits() is True

    def test_fetch_failure_returns_false(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _fail("fetch error"),
            ]
            assert push_local_commits() is False

    def test_fetch_failure_redacts_credentials_from_logs(self):
        opaque_value = "t" * 15
        with (
            patch("pynchy.host.git_ops.utils.run_git") as run,
            patch("pynchy.host.git_ops.utils.logger.warning") as warning,
        ):
            run.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _fail(f"fatal: ssh://x-access-token:{opaque_value}@github.com/repo.git"),
            ]

            assert push_local_commits(env={"GH_TOKEN": opaque_value}) is False

        assert opaque_value not in str(warning.call_args)
        assert "x-access-token" not in str(warning.call_args)

    def test_can_suppress_git_diagnostics(self):
        with (
            patch("pynchy.host.git_ops.utils.run_git") as run,
            patch("pynchy.host.git_ops.utils.logger.warning") as warning,
        ):
            run.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _fail("private personalization content"),
            ]

            assert push_local_commits(include_diagnostics=False) is False

        warning.assert_called_once_with("push_local: git fetch failed")

    def test_rebase_fails_then_succeeds_on_retry(self):
        """First rebase fails (origin advanced), retry with fresh fetch succeeds."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # initial fetch
                _ok("1\n"),  # rev-list: 1 commit ahead
                _fail("conflict"),  # rebase fails
                _ok(),  # rebase --abort
                _ok(),  # retry fetch
                _ok(),  # rebase succeeds on second attempt
                _ok(),  # push succeeds
            ]
            assert push_local_commits() is True

    def test_rebase_fails_twice_returns_false(self):
        """Both rebase attempts fail — exhausted retries."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # initial fetch
                _ok("1\n"),  # rev-list
                _fail("conflict"),  # first rebase fails
                _ok(),  # rebase --abort
                _ok(),  # retry fetch
                _fail("still conflict"),  # second rebase fails
                _ok(),  # rebase --abort (second)
            ]
            assert push_local_commits() is False

    def test_retry_fetch_fails_returns_false(self):
        """First rebase fails, and the retry fetch also fails."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # initial fetch
                _ok("1\n"),  # rev-list
                _fail(),  # rebase fails
                _ok(),  # rebase --abort
                _fail("network error"),  # retry fetch fails
            ]
            assert push_local_commits() is False

    def test_push_failure_returns_false(self):
        """Rebase succeeds but push fails."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("1\n"),  # rev-list
                _ok(),  # rebase succeeds
                _fail("push rejected"),  # push fails
            ]
            assert push_local_commits() is False

    def test_post_rebase_check_prevents_push(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("1\n"),  # rev-list: 1 commit ahead
                _ok(),  # rebase succeeds
            ]

            assert push_local_commits(post_rebase_check=lambda: False) is False

        assert mock.call_count == 4

    def test_pre_push_check_prevents_push_after_source_validation(self):
        callbacks: list[str] = []
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok(),  # fetch
                _ok("1\n"),  # rev-list: one local commit
                _ok(),  # rebase succeeds
            ]

            assert (
                push_local_commits(
                    main_branch="main",
                    validated_source=lambda: callbacks.append("source") or "validated-sha",
                    pre_push_check=lambda: callbacks.append("pre-push") or False,
                )
                is False
            )

        assert callbacks == ["source", "pre-push"]
        assert mock.call_count == 3

    def test_detect_main_branch_uses_credential_free_environment(self):
        local_env = {"LOCAL_GIT": "safe"}
        remote_env = {"GH_TOKEN": "remote-token"}
        with (
            patch(
                "pynchy.host.git_ops.utils.git_env_without_credentials",
                return_value=local_env,
            ),
            patch("pynchy.host.git_ops.utils.run_git") as mock,
        ):
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("0\n"),  # rev-list: no local commits
            ]

            assert push_local_commits(env=remote_env) is True

        detection = mock.call_args_list[0]
        assert detection.kwargs["env"] == local_env
        assert detection.kwargs["inherit_env"] is False
        assert mock.call_args_list[1].kwargs["env"] == remote_env

    def test_keeps_remote_token_out_of_local_git_and_disables_hooks(self):
        remote_env = {"GH_TOKEN": "redacted"}
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok(),  # fetch
                _ok("validated-head\n"),  # preflight HEAD
                _ok("1\n"),  # rev-list: one local commit
                _ok("validated-head\n"),  # pre-rebase HEAD
                _ok(),  # rebase
                _ok(),  # push
            ]

            assert (
                push_local_commits(
                    env=remote_env,
                    main_branch="main",
                    expected_head="validated-head",
                    inherit_env=False,
                )
                is True
            )

        local_calls = [
            item
            for item in mock.call_args_list
            if item.args[0] in {"rev-parse", "rev-list", "rebase"}
        ]
        assert len(local_calls) == 4
        for local_call in local_calls:
            local_env = local_call.kwargs["env"]
            assert "GH_TOKEN" not in local_env
            assert local_call.kwargs["inherit_env"] is False
            local_config = {
                local_env[f"GIT_CONFIG_KEY_{index}"]: local_env[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(local_env["GIT_CONFIG_COUNT"]))
            }
            assert not local_config["credential.helper"]
            assert local_config["core.hooksPath"] == os.devnull

        assert mock.call_args_list[0].kwargs["env"] == remote_env
        assert mock.call_args_list[-1].kwargs["env"] == remote_env

    def test_identity_lookup_uses_only_clean_fixed_global_keys(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "remote-token-only")
        with (
            patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("0\n")) as run,
            patch(
                "pynchy.host.git_ops._environment.subprocess.run",
                side_effect=[_ok("Identity Name\n"), _ok("identity@example.invalid\n")],
            ) as config,
        ):
            assert push_local_commits(main_branch="main", skip_fetch=True) is True

        assert [call.args[0] for call in config.call_args_list] == [
            ["git", "config", "--global", "--get", "user.name"],
            ["git", "config", "--global", "--get", "user.email"],
        ]
        for config_call in config.call_args_list:
            discovery_env = config_call.kwargs["env"]
            assert "GH_TOKEN" not in discovery_env
            assert "GIT_CONFIG_GLOBAL" not in discovery_env
            assert discovery_env["GIT_CONFIG_NOSYSTEM"] == "1"
            assert discovery_env["GIT_TERMINAL_PROMPT"] == "0"
            assert config_call.kwargs["timeout"] == 5

        local_env = run.call_args.kwargs["env"]
        assert local_env["GIT_AUTHOR_NAME"] == "Identity Name"
        assert local_env["GIT_COMMITTER_EMAIL"] == "identity@example.invalid"

    def test_local_git_keeps_global_identity_without_credentials(self, monkeypatch, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[user]\n\tname = Identity From Global Config\n\temail = identity@example.invalid\n"
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("GH_TOKEN", "remote-token-only")

        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("0\n")) as mock:
            assert push_local_commits(main_branch="main", skip_fetch=True) is True

        local_env = mock.call_args.kwargs["env"]
        identity = run_git(
            "var",
            "GIT_COMMITTER_IDENT",
            cwd=tmp_path,
            env=local_env,
            inherit_env=False,
        )

        assert identity.returncode == 0
        assert "Identity From Global Config <identity@example.invalid>" in identity.stdout
        assert "GH_TOKEN" not in local_env
        assert local_env["GIT_AUTHOR_NAME"] == "Identity From Global Config"
        assert local_env["GIT_COMMITTER_NAME"] == "Identity From Global Config"
        assert local_env["GIT_AUTHOR_EMAIL"] == "identity@example.invalid"
        assert local_env["GIT_COMMITTER_EMAIL"] == "identity@example.invalid"
        assert local_env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert local_env["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_pushes_validated_post_rebase_source_without_rereading_head(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok(),  # fetch
                _ok("1\n"),  # rev-list: one local commit
                _ok(),  # rebase
                _ok(),  # exact-source push
            ]

            assert (
                push_local_commits(
                    main_branch="main",
                    remote="https://github.com/owner/personalization.git",
                    validated_source=lambda: "validated-rebase-sha",
                )
                is True
            )

        assert mock.call_count == 4
        push_call = mock.call_args_list[-1]
        assert push_call.args == (
            "push",
            "--no-verify",
            "https://github.com/owner/personalization.git",
            "validated-rebase-sha:refs/heads/main",
        )

    def test_skip_fetch_skips_initial_fetch(self):
        """skip_fetch=True goes straight to rev-list (after detect_main_branch)."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok("0\n"),  # rev-list (no fetch before this)
            ]
            assert push_local_commits(skip_fetch=True) is True
            # Verify only detect_main_branch + rev-list were called (no fetch)
            assert mock.call_count == 2
            assert "rev-list" in mock.call_args[0]

    def test_subprocess_timeout_returns_false(self):
        """Subprocess errors (e.g. timeout) are caught and return False."""
        with patch(
            "pynchy.host.git_ops.utils.run_git",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            assert push_local_commits() is False

    def test_os_error_returns_false(self):
        """OS-level errors (e.g. git not found) are caught and return False."""
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError("No such file")):
            assert push_local_commits() is False


# ---------------------------------------------------------------------------
# count_commits
# ---------------------------------------------------------------------------


class TestCountCommits:
    """Tests for the count_commits helper (rev-list --count wrapper)."""

    def test_parses_count_on_success(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("3\n")):
            assert count_commits("main..branch") == 3

    def test_empty_output_counts_as_zero(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("")):
            assert count_commits("main..branch") == 0

    def test_returns_none_on_command_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert count_commits("main..branch") is None

    def test_returns_none_on_unparseable_output(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("not-a-number\n")):
            assert count_commits("main..branch") is None
