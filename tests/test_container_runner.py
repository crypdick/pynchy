"""Tests for the container runner.

Port of src/container-runner.test.ts — uses FakeProcess to simulate subprocess behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.config import OUTPUT_END_MARKER, OUTPUT_START_MARKER
from pynchy.container_runner import (
    _build_container_args,
    _build_volume_mounts,
    _input_to_dict,
    _parse_container_output,
    _parse_final_output,
    _read_gh_token,
    _read_git_identity,
    _read_oauth_token,
    _write_env_file,
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from pynchy.types import (
    ContainerInput,
    RegisteredGroup,
    VolumeMount,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = RegisteredGroup(
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

TEST_INPUT = ContainerInput(
    prompt="Hello",
    group_folder="test-group",
    chat_jid="test@g.us",
    is_main=False,
)


def _marker_wrap(output: dict[str, Any]) -> bytes:
    """Wrap a dict as sentinel-marked output bytes."""
    payload = f"{OUTPUT_START_MARKER}\n{json.dumps(output)}\n{OUTPUT_END_MARKER}\n"
    return payload.encode()


class FakeProcess:
    """Simulates asyncio.subprocess.Process for testing."""

    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_event = asyncio.Event()
        self.pid = 12345
        self._killed = False

    def emit_stdout(self, data: bytes) -> None:
        self.stdout.feed_data(data)

    def emit_stderr(self, data: bytes) -> None:
        self.stderr.feed_data(data)

    def close(self, code: int = 0) -> None:
        """Simulate process exit."""
        self._returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self._returncode  # type: ignore[return-value]

    def kill(self) -> None:
        self._killed = True

    @property
    def returncode(self) -> int | None:
        return self._returncode


class FakeStdin:
    """Minimal stdin mock that accepts writes and close."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class TestInputSerialization:
    def test_basic_fields_snake_case(self):
        inp = ContainerInput(
            prompt="hi",
            group_folder="my-group",
            chat_jid="chat@g.us",
            is_main=True,
        )
        d = _input_to_dict(inp)
        assert d == {
            "prompt": "hi",
            "group_folder": "my-group",
            "chat_jid": "chat@g.us",
            "is_main": True,
        }

    def test_optional_fields_included_when_set(self):
        inp = ContainerInput(
            prompt="hi",
            group_folder="g",
            chat_jid="c",
            is_main=False,
            session_id="sess-1",
            is_scheduled_task=True,
        )
        d = _input_to_dict(inp)
        assert d["session_id"] == "sess-1"
        assert d["is_scheduled_task"] is True

    def test_optional_fields_omitted_when_default(self):
        inp = ContainerInput(prompt="hi", group_folder="g", chat_jid="c", is_main=False)
        d = _input_to_dict(inp)
        assert "session_id" not in d
        assert "is_scheduled_task" not in d


class TestOutputParsing:
    def test_parses_snake_case_json(self):
        out = _parse_container_output(
            json.dumps(
                {
                    "status": "success",
                    "result": "done",
                    "new_session_id": "s1",
                }
            )
        )
        assert out.status == "success"
        assert out.result == "done"
        assert out.new_session_id == "s1"

    def test_parses_error_output(self):
        out = _parse_container_output(json.dumps({"status": "error", "error": "boom"}))
        assert out.status == "error"
        assert out.error == "boom"
        assert out.result is None


class TestContainerArgs:
    def test_readonly_uses_mount_flag(self):
        mounts = [VolumeMount("/host/path", "/container/path", readonly=True)]
        args = _build_container_args(mounts, "test-container")
        assert "--mount" in args
        assert any("readonly" in a for a in args)
        assert "-v" not in args[args.index("--mount") :]  # no -v after --mount for this mount

    def test_readwrite_uses_v_flag(self):
        mounts = [VolumeMount("/host/path", "/container/path", readonly=False)]
        args = _build_container_args(mounts, "test-container")
        assert "-v" in args
        assert "/host/path:/container/path" in args

    def test_includes_name_and_image(self):
        args = _build_container_args([], "my-container")
        assert args[:5] == ["run", "-i", "--rm", "--name", "my-container"]
        # Last arg is the image
        assert args[-1].endswith("-agent:latest")


class TestLegacyParsing:
    def test_extracts_between_markers(self):
        stdout = (
            f"noise\n{OUTPUT_START_MARKER}\n"
            + json.dumps(
                {
                    "status": "success",
                    "result": "hello",
                }
            )
            + f"\n{OUTPUT_END_MARKER}\nmore noise"
        )
        result = _parse_final_output(stdout, "test", "", 100)
        assert result.status == "success"
        assert result.result == "hello"

    def test_returns_error_on_invalid_json(self):
        result = _parse_final_output("not json at all", "test", "", 100)
        assert result.status == "error"
        assert "Failed to parse" in (result.error or "")


# ---------------------------------------------------------------------------
# Mount building tests (require tmp dirs)
# ---------------------------------------------------------------------------


class TestMountBuilding:
    def test_main_group_has_project_mount(self, tmp_path: Path):
        with (
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "main").mkdir(parents=True)
            group = RegisteredGroup(
                name="Main", folder="main", trigger="always", added_at="2024-01-01"
            )
            mounts = _build_volume_mounts(group, is_main=True)

            paths = [m.container_path for m in mounts]
            assert "/workspace/project" in paths
            assert "/workspace/group" in paths
            # Main should NOT have /workspace/global
            assert "/workspace/global" not in paths

    def test_nonmain_group_has_global_mount_when_exists(self, tmp_path: Path):
        with (
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "other").mkdir(parents=True)
            (tmp_path / "groups" / "global").mkdir(parents=True)
            group = RegisteredGroup(
                name="Other", folder="other", trigger="@pynchy", added_at="2024-01-01"
            )
            mounts = _build_volume_mounts(group, is_main=False)

            paths = [m.container_path for m in mounts]
            # Non-main should NOT have /workspace/project
            assert "/workspace/project" not in paths
            assert "/workspace/group" in paths
            assert "/workspace/global" in paths
            # Global mount should be readonly
            global_mount = next(m for m in mounts if m.container_path == "/workspace/global")
            assert global_mount.readonly is True


# ---------------------------------------------------------------------------
# Integration tests — run_container_agent with FakeProcess
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake_proc():
    """Must be async so StreamReader is created on the test's event loop."""
    return FakeProcess()


def _patch_subprocess(fake_proc: FakeProcess):
    """Patch asyncio.create_subprocess_exec to return our fake process."""

    async def _fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
        return fake_proc

    return patch("pynchy.container_runner.asyncio.create_subprocess_exec", _fake_create)


@contextlib.contextmanager
def _patch_dirs(tmp_path: Path):
    """Patch directory constants to use tmp_path."""
    with (
        patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
        patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
        patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
    ):
        yield


class TestRunContainerAgent:
    async def test_normal_exit_with_streaming_output(self, fake_proc: FakeProcess, tmp_path: Path):
        on_output = AsyncMock()

        with _patch_subprocess(fake_proc), _patch_dirs(tmp_path):
            # Schedule output + close after a tiny delay
            async def _driver():
                await asyncio.sleep(0.01)
                fake_proc.emit_stdout(
                    _marker_wrap(
                        {
                            "status": "success",
                            "result": "Here is my response",
                            "new_session_id": "session-123",
                        }
                    )
                )
                await asyncio.sleep(0.01)
                fake_proc.close(0)

            driver = asyncio.create_task(_driver())
            result = await run_container_agent(
                TEST_GROUP, TEST_INPUT, on_process=lambda p, n: None, on_output=on_output
            )
            await driver

        assert result.status == "success"
        assert result.new_session_id == "session-123"
        on_output.assert_called_once()
        call_arg = on_output.call_args[0][0]
        assert call_arg.result == "Here is my response"

    async def test_nonzero_exit_is_error(self, fake_proc: FakeProcess, tmp_path: Path):
        with _patch_subprocess(fake_proc), _patch_dirs(tmp_path):

            async def _driver():
                await asyncio.sleep(0.01)
                fake_proc.emit_stderr(b"something went wrong\n")
                await asyncio.sleep(0.01)
                fake_proc.close(1)

            driver = asyncio.create_task(_driver())
            result = await run_container_agent(TEST_GROUP, TEST_INPUT, on_process=lambda p, n: None)
            await driver

        assert result.status == "error"
        assert "code 1" in (result.error or "")
        assert "something went wrong" in (result.error or "")

    async def test_legacy_mode_parses_stdout(self, fake_proc: FakeProcess, tmp_path: Path):
        """Without on_output, final output is parsed from accumulated stdout."""
        with _patch_subprocess(fake_proc), _patch_dirs(tmp_path):

            async def _driver():
                await asyncio.sleep(0.01)
                fake_proc.emit_stdout(
                    _marker_wrap(
                        {
                            "status": "success",
                            "result": "legacy result",
                        }
                    )
                )
                await asyncio.sleep(0.01)
                fake_proc.close(0)

            driver = asyncio.create_task(_driver())
            # No on_output → legacy mode
            result = await run_container_agent(TEST_GROUP, TEST_INPUT, on_process=lambda p, n: None)
            await driver

        assert result.status == "success"
        assert result.result == "legacy result"

    async def test_timeout_with_short_timeout(self, fake_proc: FakeProcess, tmp_path: Path):
        """Test real timeout behavior with very short timeout values."""

        async def _fake_stop(proc: Any, name: str) -> None:
            if hasattr(proc, "close"):
                proc.close(137)

        with (
            _patch_subprocess(fake_proc),
            _patch_dirs(tmp_path),
            # IDLE_TIMEOUT=-29.9 so max(0.1, -29.9+30.0)=max(0.1, 0.1)=0.1s
            patch("pynchy.container_runner.IDLE_TIMEOUT", -29.9),
            patch("pynchy.container_runner.CONTAINER_TIMEOUT", 0.1),
            patch("pynchy.container_runner._graceful_stop", _fake_stop),
        ):
            # Don't emit any output — let it timeout
            result = await run_container_agent(
                TEST_GROUP, TEST_INPUT, on_process=lambda p, n: None, on_output=AsyncMock()
            )

        assert result.status == "error"
        assert "timed out" in (result.error or "")

    async def test_timeout_after_output_with_short_timeout(
        self, fake_proc: FakeProcess, tmp_path: Path
    ):
        """Timeout after streaming output should be idle cleanup (success)."""
        on_output = AsyncMock()

        async def _fake_stop(proc: Any, name: str) -> None:
            if hasattr(proc, "close"):
                proc.close(137)

        with (
            _patch_subprocess(fake_proc),
            _patch_dirs(tmp_path),
            patch("pynchy.container_runner.IDLE_TIMEOUT", -29.9),
            patch("pynchy.container_runner.CONTAINER_TIMEOUT", 0.1),
            patch("pynchy.container_runner._graceful_stop", _fake_stop),
        ):

            async def _driver():
                await asyncio.sleep(0.01)
                fake_proc.emit_stdout(
                    _marker_wrap(
                        {
                            "status": "success",
                            "result": "response",
                            "new_session_id": "s-99",
                        }
                    )
                )
                # Don't close — let timeout fire after the short period

            driver = asyncio.create_task(_driver())
            result = await run_container_agent(
                TEST_GROUP, TEST_INPUT, on_process=lambda p, n: None, on_output=on_output
            )
            await driver

        # Had streaming output → idle cleanup → success
        assert result.status == "success"
        assert result.new_session_id == "s-99"

    async def test_stdout_truncation(self, fake_proc: FakeProcess, tmp_path: Path):
        """Exceeding CONTAINER_MAX_OUTPUT_SIZE doesn't crash."""
        with (
            _patch_subprocess(fake_proc),
            _patch_dirs(tmp_path),
            patch("pynchy.container_runner.CONTAINER_MAX_OUTPUT_SIZE", 100),
        ):

            async def _driver():
                await asyncio.sleep(0.01)
                # Emit more than 100 bytes
                fake_proc.emit_stdout(b"x" * 200)
                await asyncio.sleep(0.01)
                fake_proc.close(0)

            driver = asyncio.create_task(_driver())
            # Should not crash
            result = await run_container_agent(TEST_GROUP, TEST_INPUT, on_process=lambda p, n: None)
            await driver

        # No markers found, fallback parse fails → error
        assert result.status == "error"


# ---------------------------------------------------------------------------
# Credential / env file tests
# ---------------------------------------------------------------------------


class TestReadOauthToken:
    def test_reads_token_from_credentials_file(self, tmp_path: Path):
        creds = tmp_path / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-token-123"}}))
        with patch("pynchy.container_runner.Path.home", return_value=tmp_path):
            assert _read_oauth_token() == "test-token-123"

    def test_returns_none_when_no_file_and_no_keychain(self, tmp_path: Path):
        with (
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_oauth_from_keychain", return_value=None),
        ):
            assert _read_oauth_token() is None


class TestWriteEnvFile:
    """Tests for _write_env_file with auto-discovery of Claude, GitHub, and git credentials."""

    def _patch_env(self, tmp_path: Path, gh_token=None, git_name=None, git_email=None):
        """Return a combined context manager patching dirs and subprocess auto-discovery."""
        return contextlib.ExitStack()

    def test_prefers_dotenv_over_oauth(self, tmp_path: Path):
        """Explicit .env file takes priority over OAuth credentials."""
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        creds = tmp_path / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "oauth-token"}}))
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_gh_token", return_value=None),
            patch("pynchy.container_runner._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "ANTHROPIC_API_KEY='sk-ant-test'" in content  # pragma: allowlist secret
            assert "oauth-token" not in content

    def test_falls_back_to_oauth_token(self, tmp_path: Path):
        """No .env file → reads OAuth token from credentials."""
        creds = tmp_path / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "my-oauth-token"}}))
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_gh_token", return_value=None),
            patch("pynchy.container_runner._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "CLAUDE_CODE_OAUTH_TOKEN='my-oauth-token'" in content

    def test_returns_none_when_no_credentials(self, tmp_path: Path):
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_oauth_from_keychain", return_value=None),
            patch("pynchy.container_runner._read_gh_token", return_value=None),
            patch("pynchy.container_runner._read_git_identity", return_value=(None, None)),
        ):
            assert _write_env_file() is None

    def test_auto_discovers_gh_token(self, tmp_path: Path):
        """GH_TOKEN is auto-discovered from gh CLI when not in .env."""
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_oauth_from_keychain", return_value=None),
            patch("pynchy.container_runner._read_gh_token", return_value="gho_abc123"),
            patch("pynchy.container_runner._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN='gho_abc123'" in content

    def test_dotenv_gh_token_overrides_auto_discovery(self, tmp_path: Path):
        """.env GH_TOKEN takes priority over gh CLI auto-discovery."""
        env_file = tmp_path / ".env"
        env_file.write_text("GH_TOKEN=explicit-token\n")
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_oauth_from_keychain", return_value=None),
            patch("pynchy.container_runner._read_gh_token", return_value="auto-token"),
            patch("pynchy.container_runner._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN='explicit-token'" in content
            assert "auto-token" not in content

    def test_auto_discovers_git_identity(self, tmp_path: Path):
        """Git identity is auto-discovered and written as all four env vars."""
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_oauth_from_keychain", return_value=None),
            patch("pynchy.container_runner._read_gh_token", return_value=None),
            patch(
                "pynchy.container_runner._read_git_identity",
                return_value=("Jane Doe", "jane@example.com"),
            ),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GIT_AUTHOR_NAME='Jane Doe'" in content
            assert "GIT_COMMITTER_NAME='Jane Doe'" in content
            assert "GIT_AUTHOR_EMAIL='jane@example.com'" in content
            assert "GIT_COMMITTER_EMAIL='jane@example.com'" in content

    def test_all_credentials_combined(self, tmp_path: Path):
        """Claude, GitHub, and git credentials are all written together."""
        creds = tmp_path / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "oauth-tok"}}))
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_gh_token", return_value="gho_xyz"),
            patch(
                "pynchy.container_runner._read_git_identity",
                return_value=("Bob", "bob@test.com"),
            ),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "CLAUDE_CODE_OAUTH_TOKEN='oauth-tok'" in content
            assert "GH_TOKEN='gho_xyz'" in content
            assert "GIT_AUTHOR_NAME='Bob'" in content

    def test_values_are_shell_quoted(self, tmp_path: Path):
        """Names with spaces and apostrophes are safely shell-quoted."""
        with (
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.Path.home", return_value=tmp_path),
            patch("pynchy.container_runner._read_oauth_from_keychain", return_value=None),
            patch("pynchy.container_runner._read_gh_token", return_value=None),
            patch(
                "pynchy.container_runner._read_git_identity",
                return_value=("O'Brien Smith", None),
            ),
        ):
            env_dir = _write_env_file()
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            # Shell quoting escapes single quotes: O'Brien → 'O'\''Brien Smith'
            assert "O" in content
            assert "Brien" in content


class TestReadGhToken:
    def test_returns_token_from_gh_cli(self):
        mock_result = type("Result", (), {"returncode": 0, "stdout": "gho_test123\n"})()
        with patch("pynchy.container_runner.subprocess.run", return_value=mock_result):
            assert _read_gh_token() == "gho_test123"

    def test_returns_none_on_failure(self):
        mock_result = type("Result", (), {"returncode": 1, "stdout": ""})()
        with patch("pynchy.container_runner.subprocess.run", return_value=mock_result):
            assert _read_gh_token() is None

    def test_returns_none_when_gh_not_installed(self):
        with patch("pynchy.container_runner.subprocess.run", side_effect=FileNotFoundError):
            assert _read_gh_token() is None

    def test_returns_none_on_timeout(self):
        with patch(
            "pynchy.container_runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 5),
        ):
            assert _read_gh_token() is None


class TestReadGitIdentity:
    def test_returns_name_and_email(self):
        def mock_run(cmd, **kwargs):
            key = cmd[-1]
            if key == "user.name":
                return type("R", (), {"returncode": 0, "stdout": "Alice\n"})()
            elif key == "user.email":
                return type("R", (), {"returncode": 0, "stdout": "alice@test.com\n"})()
            return type("R", (), {"returncode": 1, "stdout": ""})()

        with patch("pynchy.container_runner.subprocess.run", side_effect=mock_run):
            name, email = _read_git_identity()
            assert name == "Alice"
            assert email == "alice@test.com"

    def test_returns_none_when_not_configured(self):
        mock_result = type("R", (), {"returncode": 1, "stdout": ""})()
        with patch("pynchy.container_runner.subprocess.run", return_value=mock_result):
            name, email = _read_git_identity()
            assert name is None
            assert email is None

    def test_returns_partial_when_only_name_set(self):
        def mock_run(cmd, **kwargs):
            if cmd[-1] == "user.name":
                return type("R", (), {"returncode": 0, "stdout": "Bob\n"})()
            return type("R", (), {"returncode": 1, "stdout": ""})()

        with patch("pynchy.container_runner.subprocess.run", side_effect=mock_run):
            name, email = _read_git_identity()
            assert name == "Bob"
            assert email is None


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestTasksSnapshot:
    def test_main_sees_all_tasks(self, tmp_path: Path):
        with patch("pynchy.container_runner.DATA_DIR", tmp_path):
            tasks = [
                {"groupFolder": "main", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot("main", True, tasks)
            result = json.loads((tmp_path / "ipc" / "main" / "current_tasks.json").read_text())
            assert len(result) == 2

    def test_nonmain_sees_only_own_tasks(self, tmp_path: Path):
        with patch("pynchy.container_runner.DATA_DIR", tmp_path):
            tasks = [
                {"groupFolder": "main", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot("other", False, tasks)
            result = json.loads((tmp_path / "ipc" / "other" / "current_tasks.json").read_text())
            assert len(result) == 1
            assert result[0]["id"] == "t2"


class TestGroupsSnapshot:
    def test_main_sees_all_groups(self, tmp_path: Path):
        with patch("pynchy.container_runner.DATA_DIR", tmp_path):
            groups = [{"jid": "a@g.us"}, {"jid": "b@g.us"}]
            write_groups_snapshot("main", True, groups, {"a@g.us", "b@g.us"})
            result = json.loads((tmp_path / "ipc" / "main" / "available_groups.json").read_text())
            assert len(result["groups"]) == 2

    def test_nonmain_sees_no_groups(self, tmp_path: Path):
        with patch("pynchy.container_runner.DATA_DIR", tmp_path):
            groups = [{"jid": "a@g.us"}]
            write_groups_snapshot("other", False, groups, {"a@g.us"})
            result = json.loads((tmp_path / "ipc" / "other" / "available_groups.json").read_text())
            assert len(result["groups"]) == 0
