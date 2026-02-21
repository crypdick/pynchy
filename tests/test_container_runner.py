"""Tests for the container runner. Uses FakeProcess to simulate subprocess behavior."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings
from pydantic import SecretStr

from pynchy.config import GatewayConfig, Settings
from pynchy.container_runner._credentials import (
    _read_gh_token,
    _read_git_identity,
    _read_oauth_token,
    _shell_quote,
    _write_env_file,
)
from pynchy.container_runner._logging import _parse_final_output
from pynchy.container_runner._mounts import _build_container_args, _build_volume_mounts
from pynchy.container_runner._orchestrator import (
    _determine_result,
    resolve_agent_core,
    run_container_agent,
)
from pynchy.container_runner._process import StreamState
from pynchy.container_runner._serialization import _input_to_dict, _parse_container_output
from pynchy.container_runner._session_prep import (
    _is_skill_selected,
    _parse_skill_tier,
    _sync_skills,
    _write_settings_json,
)
from pynchy.container_runner._snapshots import write_groups_snapshot, write_tasks_snapshot
from pynchy.git_ops.repo import RepoContext
from pynchy.types import (
    ContainerInput,
    VolumeMount,
    WorkspaceProfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

TEST_INPUT = ContainerInput(
    messages=[
        {
            "message_type": "user",
            "sender": "user@s.whatsapp.net",
            "sender_name": "User",
            "content": "Hello",
            "timestamp": "2024-01-01T00:00:00.000Z",
            "metadata": None,
        }
    ],
    group_folder="test-group",
    chat_jid="test@g.us",
    is_admin=False,
)


def _marker_wrap(output: dict[str, Any]) -> bytes:
    """Wrap a dict as sentinel-marked output bytes."""
    payload = (
        f"{Settings.OUTPUT_START_MARKER}\n{json.dumps(output)}\n{Settings.OUTPUT_END_MARKER}\n"
    )
    return payload.encode()


_CR_CREDS = "pynchy.container_runner._credentials"
_CR_ORCH = "pynchy.container_runner._orchestrator"
_GATEWAY = "pynchy.container_runner.gateway"


class _MockGateway:
    """Lightweight stand-in for ``gateway.Gateway`` in credential tests."""

    def __init__(self, providers: set[str] | None = None) -> None:
        self.base_url = "http://host.docker.internal:4010"
        self.key = "gw-test-key"
        self._providers = providers or set()

    def has_provider(self, name: str) -> bool:
        return name in self._providers


_SETTINGS_MODULES = [
    _CR_CREDS,
    "pynchy.container_runner._mounts",
    "pynchy.container_runner._session_prep",
    _CR_ORCH,
    "pynchy.container_runner._snapshots",
]


@contextlib.contextmanager
def _patch_settings(
    tmp_path: Path | None = None,
    *,
    core: str | None = None,
    container_timeout: float | None = None,
    idle_timeout: float | None = None,
    max_output_size: int | None = None,
    secret_overrides: dict[str, str] | None = None,
):
    """Patch get_settings() across all container_runner submodules."""
    overrides: dict = {"gateway": GatewayConfig()}
    if tmp_path is not None:
        overrides.update(
            project_root=tmp_path,
            groups_dir=tmp_path / "groups",
            data_dir=tmp_path / "data",
        )
    if container_timeout is not None:
        overrides["container_timeout"] = container_timeout
    if idle_timeout is not None:
        overrides["idle_timeout"] = idle_timeout
    s = make_settings(**overrides)
    if core is not None:
        s.agent.core = core
    if max_output_size is not None:
        s.container.max_output_size = max_output_size
    if secret_overrides:
        for key, value in secret_overrides.items():
            setattr(s.secrets, key, SecretStr(value))
    with contextlib.ExitStack() as stack:
        for mod in _SETTINGS_MODULES:
            stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
        yield


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
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="my-group",
            chat_jid="chat@g.us",
            is_admin=True,
        )
        d = _input_to_dict(inp)
        assert d == {
            "messages": [{"message_type": "user", "content": "hi"}],
            "group_folder": "my-group",
            "chat_jid": "chat@g.us",
            "is_admin": True,
            "agent_core_module": "agent_runner.cores.claude",
            "agent_core_class": "ClaudeAgentCore",
        }

    def test_optional_fields_included_when_set(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
            session_id="sess-1",
            is_scheduled_task=True,
        )
        d = _input_to_dict(inp)
        assert d["session_id"] == "sess-1"
        assert d["is_scheduled_task"] is True

    def test_optional_fields_omitted_when_default(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
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
        assert args[:4] == ["run", "-i", "--name", "my-container"]
        # Last arg is the image
        assert args[-1].endswith("-agent:latest")


class TestLegacyParsing:
    def test_extracts_between_markers(self):
        stdout = (
            f"noise\n{Settings.OUTPUT_START_MARKER}\n"
            + json.dumps(
                {
                    "status": "success",
                    "result": "hello",
                }
            )
            + f"\n{Settings.OUTPUT_END_MARKER}\nmore noise"
        )
        result = _parse_final_output(stdout, "test", "", 100)
        assert result.status == "success"
        assert result.result == "hello"

    def test_returns_error_on_invalid_json(self):
        result = _parse_final_output("not json at all", "test", "", 100)
        assert result.status == "error"
        assert "Invalid JSON" in (result.error or "")


# ---------------------------------------------------------------------------
# Mount building tests (require tmp dirs)
# ---------------------------------------------------------------------------


class TestMountBuilding:
    def test_god_group_has_project_mount(self, tmp_path: Path):
        worktree_path = tmp_path / "worktrees" / "god"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "god").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="god@g.us", name="God", folder="god", trigger="always", added_at="2024-01-01"
            )
            mounts = _build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            paths = [m.container_path for m in mounts]
            assert "/workspace/project" in paths
            assert "/workspace/group" in paths
            # God should NOT have /workspace/global
            assert "/workspace/global" not in paths

    def test_nongod_group_has_no_global_mount(self, tmp_path: Path):
        """Non-admin groups no longer get a /workspace/global mount.

        Directives replaced the old global CLAUDE.md overlay — content is now
        resolved host-side and passed via system_prompt_append.
        """
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "other").mkdir(parents=True)
            (tmp_path / "groups" / "global").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="other@g.us",
                name="Other",
                folder="other",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = _build_volume_mounts(group, is_admin=False)

            paths = [m.container_path for m in mounts]
            assert "/workspace/project" not in paths
            assert "/workspace/group" in paths
            assert "/workspace/global" not in paths

    def test_nongod_repo_access_uses_worktree_path(self, tmp_path: Path):
        """Non-admin group with repo_access + worktree_path mounts the worktree."""
        worktree_path = tmp_path / "worktrees" / "code-improver"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )

        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "code-improver").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="code-improver@g.us",
                name="Code Improver",
                folder="code-improver",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = _build_volume_mounts(
                group, is_admin=False, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            project_mount = next(m for m in mounts if m.container_path == "/workspace/project")
            assert project_mount.host_path == str(worktree_path)
            assert project_mount.readonly is False

            # .git dir mounted at host path so worktree gitdir reference resolves
            git_mount = next(m for m in mounts if m.host_path == str(tmp_path / ".git"))
            assert git_mount.container_path == str(tmp_path / ".git")

    def test_god_uses_worktree(self, tmp_path: Path):
        """Admin group uses worktree just like any other repo_access group."""
        worktree_path = tmp_path / "worktrees" / "god"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "god").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="god@g.us", name="God", folder="god", trigger="always", added_at="2024-01-01"
            )
            mounts = _build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            project_mount = next(m for m in mounts if m.container_path == "/workspace/project")
            assert project_mount.host_path == str(worktree_path)
            assert project_mount.readonly is False

    def test_god_gets_config_toml_mount(self, tmp_path: Path):
        """Admin group gets config.toml mounted read-write when it exists."""
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "god").mkdir(parents=True)
            (tmp_path / "config.toml").write_text("[agent]\nname = 'pynchy'\n")
            group = WorkspaceProfile(
                jid="god@g.us", name="God", folder="god", trigger="always", added_at="2024-01-01"
            )
            mounts = _build_volume_mounts(group, is_admin=True)

            config_mount = next(
                m for m in mounts if m.container_path == "/workspace/project/config.toml"
            )
            assert config_mount.host_path == str(tmp_path / "config.toml")
            assert config_mount.readonly is False

    def test_nongod_does_not_get_config_toml(self, tmp_path: Path):
        """Non-admin groups never get config.toml mounted."""
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "other").mkdir(parents=True)
            (tmp_path / "config.toml").write_text("[agent]\nname = 'pynchy'\n")
            group = WorkspaceProfile(
                jid="other@g.us",
                name="Other",
                folder="other",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = _build_volume_mounts(group, is_admin=False)

            paths = [m.container_path for m in mounts]
            assert "/workspace/project/config.toml" not in paths

    def test_god_no_config_toml_when_missing(self, tmp_path: Path):
        """Admin group doesn't get config.toml mount if the file doesn't exist."""
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "god").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="god@g.us", name="God", folder="god", trigger="always", added_at="2024-01-01"
            )
            mounts = _build_volume_mounts(group, is_admin=True)

            paths = [m.container_path for m in mounts]
            assert "/workspace/project/config.toml" not in paths


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

    return patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", _fake_create)


@contextlib.contextmanager
def _patch_dirs(tmp_path: Path):
    """Patch directory settings to use tmp_path."""
    with _patch_settings(tmp_path):
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
            # idle_timeout=-29.9 and container_timeout=0.1:
            # max(0.1, -29.9 + 30.0) == 0.1s
            _patch_settings(tmp_path, idle_timeout=-29.9, container_timeout=0.1),
            patch(f"{_CR_ORCH}._graceful_stop", _fake_stop),
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
            _patch_settings(tmp_path, idle_timeout=-29.9, container_timeout=0.1),
            patch(f"{_CR_ORCH}._graceful_stop", _fake_stop),
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
            _patch_settings(tmp_path, max_output_size=100),
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
        with patch(f"{_CR_CREDS}.Path.home", return_value=tmp_path):
            assert _read_oauth_token() == "test-token-123"

    def test_returns_none_when_no_file_and_no_keychain(self, tmp_path: Path):
        with (
            patch(f"{_CR_CREDS}.Path.home", return_value=tmp_path),
            patch(f"{_CR_CREDS}._read_oauth_from_keychain", return_value=None),
        ):
            assert _read_oauth_token() is None


class TestWriteEnvFile:
    """Tests for _write_env_file with auto-discovery of Claude, GitHub, and git credentials."""

    def _patch_env(self, tmp_path: Path, gh_token=None, git_name=None, git_email=None):
        """Return a combined context manager patching dirs and subprocess auto-discovery."""
        return contextlib.ExitStack()

    def test_gateway_writes_anthropic_proxy_vars(self, tmp_path: Path):
        """When gateway has anthropic, env gets ANTHROPIC_BASE_URL + AUTH_TOKEN."""
        gw = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value=None),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert f"ANTHROPIC_BASE_URL='{gw.base_url}'" in content
            assert f"ANTHROPIC_AUTH_TOKEN='{gw.key}'" in content
            # Real keys must never appear
            assert "sk-ant" not in content
            assert "oauth" not in content

    def test_gateway_writes_openai_proxy_vars(self, tmp_path: Path):
        """When gateway has openai, env gets OPENAI_BASE_URL + OPENAI_API_KEY."""
        gw = _MockGateway(providers={"openai"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value=None),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert f"OPENAI_BASE_URL='{gw.base_url}'" in content
            assert f"OPENAI_API_KEY='{gw.key}'" in content

    def test_returns_none_when_no_credentials(self, tmp_path: Path):
        """No gateway providers and no non-LLM creds → returns None."""
        gw = _MockGateway(providers=set())
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value=None),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            assert _write_env_file(is_admin=True, group_folder="test") is None

    def test_auto_discovers_gh_token_for_god(self, tmp_path: Path):
        """GH_TOKEN is auto-discovered from gh CLI for god containers."""
        gw = _MockGateway(providers=set())
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value="gho_abc123"),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN='gho_abc123'" in content

    def test_non_god_excludes_gh_token(self, tmp_path: Path):
        """Non-admin containers never receive GH_TOKEN, even when available."""
        gw = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path, secret_overrides={"gh_token": "explicit-token"}),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value="gho_abc123"),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file(is_admin=False, group_folder="untrusted")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN" not in content
            assert "ANTHROPIC_BASE_URL" in content

    def test_settings_gh_token_overrides_auto_discovery(self, tmp_path: Path):
        """Configured GH_TOKEN takes priority over gh CLI auto-discovery."""
        gw = _MockGateway(providers=set())
        with (
            _patch_settings(tmp_path, secret_overrides={"gh_token": "explicit-token"}),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value="auto-token"),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GH_TOKEN='explicit-token'" in content
            assert "auto-token" not in content

    def test_auto_discovers_git_identity(self, tmp_path: Path):
        """Git identity is auto-discovered and written as all four env vars."""
        gw = _MockGateway(providers=set())
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value=None),
            patch(
                f"{_CR_CREDS}._read_git_identity",
                return_value=("Jane Doe", "jane@example.com"),
            ),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GIT_AUTHOR_NAME='Jane Doe'" in content
            assert "GIT_COMMITTER_NAME='Jane Doe'" in content
            assert "GIT_AUTHOR_EMAIL='jane@example.com'" in content
            assert "GIT_COMMITTER_EMAIL='jane@example.com'" in content

    def test_all_credentials_combined(self, tmp_path: Path):
        """Gateway LLM creds, GitHub, and git credentials are all written together."""
        gw = _MockGateway(providers={"anthropic", "openai"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value="gho_xyz"),
            patch(
                f"{_CR_CREDS}._read_git_identity",
                return_value=("Bob", "bob@test.com"),
            ),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert f"ANTHROPIC_BASE_URL='{gw.base_url}'" in content
            assert f"ANTHROPIC_AUTH_TOKEN='{gw.key}'" in content
            assert f"OPENAI_BASE_URL='{gw.base_url}'" in content
            assert f"OPENAI_API_KEY='{gw.key}'" in content
            assert "GH_TOKEN='gho_xyz'" in content
            assert "GIT_AUTHOR_NAME='Bob'" in content

    def test_per_group_env_dirs_are_isolated(self, tmp_path: Path):
        """Each group gets its own env directory."""
        gw = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value="gho_xyz"),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            god_dir = _write_env_file(is_admin=True, group_folder="god-group")
            nongod_dir = _write_env_file(is_admin=False, group_folder="other-group")
            assert god_dir != nongod_dir
            assert "GH_TOKEN" in (god_dir / "env").read_text()
            assert "GH_TOKEN" not in (nongod_dir / "env").read_text()

    def test_values_are_shell_quoted(self, tmp_path: Path):
        """Names with spaces and apostrophes are safely shell-quoted."""
        gw = _MockGateway(providers=set())
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value=None),
            patch(
                f"{_CR_CREDS}._read_git_identity",
                return_value=("O'Brien Smith", None),
            ),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            # Shell quoting escapes single quotes: O'Brien → 'O'\''Brien Smith'
            assert "O" in content
            assert "Brien" in content


class TestReadGhToken:
    def test_returns_token_from_gh_cli(self):
        mock_result = type("Result", (), {"returncode": 0, "stdout": "gho_test123\n"})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            assert _read_gh_token() == "gho_test123"

    def test_returns_none_on_failure(self):
        mock_result = type("Result", (), {"returncode": 1, "stdout": ""})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            assert _read_gh_token() is None

    def test_returns_none_when_gh_not_installed(self):
        with patch(f"{_CR_CREDS}.subprocess.run", side_effect=FileNotFoundError):
            assert _read_gh_token() is None

    def test_returns_none_on_timeout(self):
        with patch(
            f"{_CR_CREDS}.subprocess.run",
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

        with patch(f"{_CR_CREDS}.subprocess.run", side_effect=mock_run):
            name, email = _read_git_identity()
            assert name == "Alice"
            assert email == "alice@test.com"

    def test_returns_none_when_not_configured(self):
        mock_result = type("R", (), {"returncode": 1, "stdout": ""})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            name, email = _read_git_identity()
            assert name is None
            assert email is None

    def test_returns_partial_when_only_name_set(self):
        def mock_run(cmd, **kwargs):
            if cmd[-1] == "user.name":
                return type("R", (), {"returncode": 0, "stdout": "Bob\n"})()
            return type("R", (), {"returncode": 1, "stdout": ""})()

        with patch(f"{_CR_CREDS}.subprocess.run", side_effect=mock_run):
            name, email = _read_git_identity()
            assert name == "Bob"
            assert email is None


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestTasksSnapshot:
    def test_god_sees_all_tasks(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [
                {"groupFolder": "god", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot("god", True, tasks)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "god" / "current_tasks.json").read_text()
            )
            assert len(result) == 2

    def test_nongod_sees_only_own_tasks(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [
                {"groupFolder": "god", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot("other", False, tasks)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "current_tasks.json").read_text()
            )
            assert len(result) == 1
            assert result[0]["id"] == "t2"

    def test_god_includes_host_jobs(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [{"groupFolder": "god", "id": "t1"}]
            host_jobs = [{"type": "host", "id": "h1", "name": "daily-backup"}]
            write_tasks_snapshot("god", True, tasks, host_jobs=host_jobs)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "god" / "current_tasks.json").read_text()
            )
            assert len(result) == 2
            assert result[0]["id"] == "t1"
            assert result[1]["id"] == "h1"
            assert result[1]["type"] == "host"

    def test_nongod_ignores_host_jobs(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [{"groupFolder": "other", "id": "t1"}]
            host_jobs = [{"type": "host", "id": "h1", "name": "daily-backup"}]
            write_tasks_snapshot("other", False, tasks, host_jobs=host_jobs)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "current_tasks.json").read_text()
            )
            assert len(result) == 1
            assert result[0]["id"] == "t1"


class TestGroupsSnapshot:
    def test_god_sees_all_groups(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            groups = [{"jid": "a@g.us"}, {"jid": "b@g.us"}]
            write_groups_snapshot("god", True, groups, {"a@g.us", "b@g.us"})
            result = json.loads(
                (tmp_path / "data" / "ipc" / "god" / "available_groups.json").read_text()
            )
            assert len(result["groups"]) == 2

    def test_nongod_sees_no_groups(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            groups = [{"jid": "a@g.us"}]
            write_groups_snapshot("other", False, groups, {"a@g.us"})
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "available_groups.json").read_text()
            )
            assert len(result["groups"]) == 0


# ---------------------------------------------------------------------------
# resolve_agent_core
# ---------------------------------------------------------------------------


class TestResolveAgentCore:
    """Test agent core resolution from plugin manager.

    This selects which AI agent core (module + class) to use for container
    execution. Getting this wrong silently breaks all agent runs.
    """

    def test_returns_defaults_when_no_plugin_manager(self):
        module, cls = resolve_agent_core(None)
        assert module == "agent_runner.cores.claude"
        assert cls == "ClaudeAgentCore"

    def test_returns_defaults_when_plugin_manager_is_falsy(self):
        """Covers the `if plugin_manager:` guard for falsy values like False/0."""
        module, cls = resolve_agent_core(False)
        assert module == "agent_runner.cores.claude"
        assert cls == "ClaudeAgentCore"

    def test_returns_defaults_when_no_cores_registered(self):
        """Plugin manager exists but no agent core plugins are installed."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return []

        class FakePM:
            hook = FakeHook()

        module, cls = resolve_agent_core(FakePM())
        assert module == "agent_runner.cores.claude"
        assert cls == "ClaudeAgentCore"

    def test_uses_matching_core_by_name(self):
        """When a core matches DEFAULT_AGENT_CORE, use it."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    {"name": "openai", "module": "cores.openai", "class_name": "OpenAICore"},
                    {"name": "claude", "module": "cores.claude_v2", "class_name": "ClaudeV2Core"},
                ]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(core="claude"):
            module, cls = resolve_agent_core(FakePM())

        assert module == "cores.claude_v2"
        assert cls == "ClaudeV2Core"

    def test_falls_back_to_first_core_when_no_name_match(self):
        """If the configured DEFAULT_AGENT_CORE doesn't match any plugin, use the first one."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    {"name": "openai", "module": "cores.openai", "class_name": "OpenAICore"},
                    {"name": "gemini", "module": "cores.gemini", "class_name": "GeminiCore"},
                ]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(core="claude"):
            module, cls = resolve_agent_core(FakePM())

        assert module == "cores.openai"
        assert cls == "OpenAICore"

    def test_exact_match_takes_priority_over_first(self):
        """When the desired core is second in the list, it still wins over first."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    {"name": "openai", "module": "cores.openai", "class_name": "OpenAICore"},
                    {"name": "custom", "module": "cores.custom", "class_name": "CustomCore"},
                ]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(core="custom"):
            module, cls = resolve_agent_core(FakePM())

        assert module == "cores.custom"
        assert cls == "CustomCore"


# ---------------------------------------------------------------------------
# _sync_skills tests
# ---------------------------------------------------------------------------


class TestSyncSkills:
    """Test skill syncing from built-in skills and plugin skills into session dir."""

    def test_copies_builtin_skills(self, tmp_path: Path):
        """Built-in skills are copied to the session .claude/skills/ dir."""
        # Create a built-in skill
        builtin_skill = tmp_path / "container" / "skills" / "my-skill"
        builtin_skill.mkdir(parents=True)
        (builtin_skill / "skill.md").write_text("# My Skill\nDo stuff.")
        (builtin_skill / "config.json").write_text('{"name": "my-skill"}')

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir)

        skills_dst = session_dir / "skills" / "my-skill"
        assert skills_dst.exists()
        assert (skills_dst / "skill.md").read_text() == "# My Skill\nDo stuff."
        assert (skills_dst / "config.json").exists()

    def test_no_skills_dir_is_safe(self, tmp_path: Path):
        """Missing container/skills/ dir should not crash."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir)

        # skills/ directory should still be created (empty)
        assert (session_dir / "skills").exists()

    def test_plugin_skills_are_synced(self, tmp_path: Path):
        """Plugin manager skill paths are copied to session dir."""
        plugin_skill = tmp_path / "plugins" / "ext-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "skill.md").write_text("# External Skill")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, plugin_manager=FakePM())

        ext_dst = session_dir / "skills" / "ext-skill"
        assert ext_dst.exists()
        assert (ext_dst / "skill.md").read_text() == "# External Skill"

    def test_plugin_skill_name_collision_raises(self, tmp_path: Path):
        """Plugin skill that shadows a built-in skill raises ValueError."""
        # Create built-in skill
        builtin_skill = tmp_path / "container" / "skills" / "my-skill"
        builtin_skill.mkdir(parents=True)
        (builtin_skill / "skill.md").write_text("built-in")

        # Create plugin skill with same name
        plugin_skill = tmp_path / "plugins" / "my-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "skill.md").write_text("plugin")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM:
            hook = FakeHook()

        with (
            _patch_settings(tmp_path),
            pytest.raises(ValueError, match="collision"),
        ):
            _sync_skills(session_dir, plugin_manager=FakePM())

    def test_skips_nonexistent_plugin_skill_path(self, tmp_path: Path):
        """Plugin skill paths that don't exist are skipped with a warning."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(tmp_path / "nonexistent-skill")]]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(tmp_path):
            # Should not crash
            _sync_skills(session_dir, plugin_manager=FakePM())

    def test_ignores_files_in_skills_dir(self, tmp_path: Path):
        """Files (not directories) in container/skills/ are ignored."""
        skills_dir = tmp_path / "container" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "README.md").write_text("not a skill dir")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir)

        # Only the skills/ directory should exist, no README.md copied
        assert not (session_dir / "skills" / "README.md").exists()


# ---------------------------------------------------------------------------
# Skill tier helpers
# ---------------------------------------------------------------------------


class TestParseSkillTier:
    """Test SKILL.md frontmatter parsing for name and tier."""

    def test_valid_frontmatter(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ntier: core\n---\n# My Skill\n")
        name, tier = _parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "core"

    def test_missing_tier_defaults_to_community(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill\n")
        name, tier = _parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_no_skill_md_defaults(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        name, tier = _parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_no_frontmatter_delimiters(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Just a heading\nNo frontmatter here.\n")
        name, tier = _parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_dev_tier(self, tmp_path: Path):
        skill_dir = tmp_path / "code-improver"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: code-improver\ntier: dev\n---\n# Code Improver\n"
        )
        name, tier = _parse_skill_tier(skill_dir)
        assert name == "code-improver"
        assert tier == "dev"

    def test_name_defaults_to_dir_name(self, tmp_path: Path):
        """When name is missing from frontmatter, use directory name."""
        skill_dir = tmp_path / "web-search"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\ntier: core\n---\n# Web Search\n")
        name, tier = _parse_skill_tier(skill_dir)
        assert name == "web-search"
        assert tier == "core"


class TestIsSkillSelected:
    """Test skill selection resolution logic."""

    def test_none_includes_everything(self):
        assert _is_skill_selected("any-skill", "community", None) is True

    def test_all_includes_everything(self):
        assert _is_skill_selected("any-skill", "community", ["all"]) is True

    def test_tier_match(self):
        assert _is_skill_selected("my-skill", "dev", ["dev"]) is True

    def test_name_match(self):
        assert _is_skill_selected("web-search", "community", ["web-search"]) is True

    def test_core_always_included_when_filtering_active(self):
        """Core tier is implicit when any filtering is set."""
        assert _is_skill_selected("browser", "core", ["dev"]) is True

    def test_community_excluded_when_not_listed(self):
        assert _is_skill_selected("some-skill", "community", ["core"]) is False

    def test_dev_excluded_when_not_listed(self):
        assert _is_skill_selected("code-improver", "dev", ["core"]) is False

    def test_union_of_tier_and_name(self):
        """Tiers and names are unioned."""
        ws = ["core", "web-search"]
        assert _is_skill_selected("web-search", "community", ws) is True
        assert _is_skill_selected("python-heredoc", "core", ws) is True
        assert _is_skill_selected("code-improver", "dev", ws) is False

    def test_empty_list_still_includes_core(self):
        """Even an empty skills list includes core (filtering is active)."""
        assert _is_skill_selected("browser", "core", []) is True
        assert _is_skill_selected("other", "community", []) is False


class TestSyncSkillsFiltering:
    """Test _sync_skills with workspace_skills filtering."""

    def _create_skill(self, base: Path, name: str, tier: str) -> None:
        skill_dir = base / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ntier: {tier}\n---\n# {name}\n")

    def test_none_copies_all_skills(self, tmp_path: Path):
        """workspace_skills=None copies everything (backwards compat)."""
        skills_src = tmp_path / "container" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=None)

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "improver", "extra"}

    def test_core_only_filters_correctly(self, tmp_path: Path):
        """workspace_skills=["core"] copies only core-tier skills."""
        skills_src = tmp_path / "container" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["core"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser"}

    def test_core_plus_dev(self, tmp_path: Path):
        """workspace_skills=["core", "dev"] copies core + dev skills."""
        skills_src = tmp_path / "container" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["core", "dev"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "improver"}

    def test_core_plus_specific_name(self, tmp_path: Path):
        """workspace_skills=["core", "extra"] includes core tier + named skill."""
        skills_src = tmp_path / "container" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["core", "extra"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "extra"}

    def test_all_copies_everything(self, tmp_path: Path):
        """workspace_skills=["all"] is equivalent to None."""
        skills_src = tmp_path / "container" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["all"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "improver"}

    def test_plugin_skills_filtered(self, tmp_path: Path):
        """Plugin skills are also filtered by workspace_skills."""
        plugin_skill = tmp_path / "plugins" / "ext-tool"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text(
            "---\nname: ext-tool\ntier: community\n---\n# External\n"
        )

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, plugin_manager=FakePM(), workspace_skills=["core"])

        # Plugin skill is community tier, should be excluded
        assert not (session_dir / "skills" / "ext-tool").exists()

    def test_plugin_skill_included_by_name(self, tmp_path: Path):
        """Plugin skill included when referenced by name."""
        plugin_skill = tmp_path / "plugins" / "ext-tool"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text(
            "---\nname: ext-tool\ntier: community\n---\n# External\n"
        )

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM:
            hook = FakeHook()

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir, plugin_manager=FakePM(), workspace_skills=["core", "ext-tool"]
            )

        assert (session_dir / "skills" / "ext-tool").exists()


# ---------------------------------------------------------------------------
# _write_settings_json tests
# ---------------------------------------------------------------------------


class TestWriteSettingsJson:
    """Test settings.json generation for Claude Code sessions."""

    def test_writes_default_settings(self, tmp_path: Path):
        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _write_settings_json(session_dir)

        settings_file = session_dir / "settings.json"
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())
        assert "env" in settings
        assert settings["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"

    def test_merges_hook_config(self, tmp_path: Path):
        """Hook settings from container/scripts/settings.json are merged."""
        scripts_dir = tmp_path / "container" / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "Bash": [
                            {
                                "matcher": "command",
                                "pattern": "git push",
                                "hook": "/workspace/scripts/guard_git.sh",
                            }
                        ]
                    }
                }
            )
        )

        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _write_settings_json(session_dir)

        settings = json.loads((session_dir / "settings.json").read_text())
        assert "hooks" in settings
        assert "Bash" in settings["hooks"]

    def test_survives_malformed_hook_config(self, tmp_path: Path):
        """Invalid JSON in hook settings doesn't crash — falls back gracefully."""
        scripts_dir = tmp_path / "container" / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "settings.json").write_text("not valid json {{{")

        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _write_settings_json(session_dir)

        settings = json.loads((session_dir / "settings.json").read_text())
        # Should still have env but no hooks
        assert "env" in settings
        assert "hooks" not in settings

    def test_overwrites_existing_settings(self, tmp_path: Path):
        """Settings are regenerated on each call to pick up hook changes."""
        session_dir = tmp_path / ".claude"
        session_dir.mkdir(parents=True)
        (session_dir / "settings.json").write_text('{"stale": true}')

        with _patch_settings(tmp_path):
            _write_settings_json(session_dir)

        settings = json.loads((session_dir / "settings.json").read_text())
        assert "stale" not in settings
        assert "env" in settings


# ---------------------------------------------------------------------------
# Shell quoting tests
# ---------------------------------------------------------------------------


class TestShellQuote:
    """Test shell quoting for env file values."""

    def test_simple_string(self):
        assert _shell_quote("hello") == "'hello'"

    def test_string_with_spaces(self):
        assert _shell_quote("hello world") == "'hello world'"

    def test_string_with_single_quotes(self):
        # O'Brien → 'O'\''Brien'
        result = _shell_quote("O'Brien")
        assert result == "'" + "O" + "'\\''" + "Brien" + "'"

    def test_empty_string(self):
        assert _shell_quote("") == "''"

    def test_string_with_special_chars(self):
        """Special shell chars should be safely quoted."""
        result = _shell_quote("$HOME && rm -rf /")
        assert result.startswith("'")
        assert result.endswith("'")
        assert "$HOME" in result


# ---------------------------------------------------------------------------
# Container output parsing edge cases
# ---------------------------------------------------------------------------


class TestOutputParsingEdgeCases:
    """Edge cases for _parse_container_output and _parse_final_output."""

    def test_parses_all_output_fields(self):
        """Verify all ContainerOutput fields are correctly parsed."""
        out = _parse_container_output(
            json.dumps(
                {
                    "status": "success",
                    "result": "done",
                    "new_session_id": "s1",
                    "type": "tool_use",
                    "thinking": "Let me think...",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/test.py"},
                    "text": "some text",
                    "system_subtype": "compact",
                    "system_data": {"key": "val"},
                    "tool_result_id": "tr-1",
                    "tool_result_content": "file contents",
                    "tool_result_is_error": False,
                    "result_metadata": {"duration_ms": 1234},
                }
            )
        )
        assert out.status == "success"
        assert out.type == "tool_use"
        assert out.thinking == "Let me think..."
        assert out.tool_name == "Read"
        assert out.tool_input == {"file_path": "/test.py"}
        assert out.system_subtype == "compact"
        assert out.tool_result_id == "tr-1"
        assert out.tool_result_is_error is False
        assert out.result_metadata == {"duration_ms": 1234}

    def test_parse_final_output_empty_stdout(self):
        """Empty stdout should return error output."""
        result = _parse_final_output("", "test-container", "", 100)
        assert result.status == "error"

    def test_parse_final_output_markers_without_json(self):
        """Markers present but content is not valid JSON."""
        stdout = f"{Settings.OUTPUT_START_MARKER}\nnot json\n{Settings.OUTPUT_END_MARKER}"
        result = _parse_final_output(stdout, "test-container", "", 100)
        assert result.status == "error"
        assert "Invalid JSON" in (result.error or "")

    def test_parse_final_output_multiple_marker_pairs(self):
        """When multiple marker pairs exist, uses the first one."""
        first = json.dumps({"status": "success", "result": "first"})
        second = json.dumps({"status": "success", "result": "second"})
        stdout = (
            f"{Settings.OUTPUT_START_MARKER}\n{first}\n{Settings.OUTPUT_END_MARKER}\n"
            f"{Settings.OUTPUT_START_MARKER}\n{second}\n{Settings.OUTPUT_END_MARKER}"
        )
        result = _parse_final_output(stdout, "test-container", "", 100)
        assert result.status == "success"
        # Uses the first marker pair
        assert result.result == "first"

    def test_parse_final_output_fallback_to_last_line(self):
        """Without markers, falls back to last non-empty line."""
        last_line = json.dumps({"status": "success", "result": "fallback"})
        stdout = f"some noise\nmore noise\n{last_line}\n"
        result = _parse_final_output(stdout, "test-container", "", 100)
        assert result.status == "success"
        assert result.result == "fallback"

    def test_parse_final_output_invalid_json_error_message(self):
        """Invalid JSON should produce a specific 'Invalid JSON' error."""
        result = _parse_final_output("{bad json", "test-container", "", 100)
        assert result.status == "error"
        assert "Invalid JSON" in (result.error or "")

    def test_parse_final_output_missing_status_key(self):
        """Valid JSON missing required 'status' key should report missing field."""
        stdout = json.dumps({"result": "no status field"})
        result = _parse_final_output(stdout, "test-container", "", 100)
        assert result.status == "error"
        assert "Missing required field" in (result.error or "") or "status" in (result.error or "")

    def test_parse_final_output_truncates_long_preview_in_error(self):
        """Very long invalid output should not flood error messages."""
        long_garbage = "x" * 500
        result = _parse_final_output(long_garbage, "test-container", "", 100)
        assert result.status == "error"
        # The error message should exist but be reasonable length
        assert len(result.error or "") < 1000


# ---------------------------------------------------------------------------
# _determine_result tests
# ---------------------------------------------------------------------------


class TestDetermineResult:
    """Tests for _determine_result — the branching logic that maps container
    run state (timeout, exit code, streaming vs legacy) to ContainerOutput."""

    def _make_state(self, **kwargs) -> StreamState:
        """Create a StreamState with test defaults."""
        return StreamState(**kwargs)

    def test_timeout_with_streaming_output_returns_success(self):
        """Timeout after streaming output = idle cleanup, not an error."""
        state = self._make_state(
            timed_out=True,
            had_streaming_output=True,
            new_session_id="session-1",
        )
        result = _determine_result(
            state=state,
            exit_code=None,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=300000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf="",
        )
        assert result.status == "success"
        assert result.new_session_id == "session-1"

    def test_timeout_without_output_returns_error(self):
        """Timeout with no output at all = real error."""
        state = self._make_state(timed_out=True, had_streaming_output=False)
        result = _determine_result(
            state=state,
            exit_code=None,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=300000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf="",
        )
        assert result.status == "error"
        assert "timed out" in (result.error or "").lower()
        assert "300" in (result.error or "")

    def test_nonzero_exit_code_returns_error(self):
        """Non-zero exit code always means error, with stderr tail in message."""
        state = self._make_state()
        result = _determine_result(
            state=state,
            exit_code=1,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=5000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf="some error output from container",
        )
        assert result.status == "error"
        assert "code 1" in (result.error or "")
        assert "some error output" in (result.error or "")

    def test_nonzero_exit_code_truncates_long_stderr(self):
        """Stderr in error message is truncated to last 200 chars."""
        state = self._make_state()
        long_stderr = "x" * 500
        result = _determine_result(
            state=state,
            exit_code=2,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=5000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf=long_stderr,
        )
        assert result.status == "error"
        # Error message includes at most 200 chars of stderr
        assert len(result.error or "") < 300

    def test_streaming_mode_success(self):
        """Streaming mode (on_output set) returns success with session ID."""
        state = self._make_state(new_session_id="session-42")
        result = _determine_result(
            state=state,
            exit_code=0,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=5000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf="",
        )
        assert result.status == "success"
        assert result.new_session_id == "session-42"
        assert result.result is None  # result delivered via callbacks

    def test_legacy_mode_parses_stdout(self):
        """Legacy mode (on_output=None) parses final output from stdout."""
        output_json = json.dumps({"status": "success", "result": "hello world"})
        stdout = f"{Settings.OUTPUT_START_MARKER}\n{output_json}\n{Settings.OUTPUT_END_MARKER}"
        state = self._make_state()
        result = _determine_result(
            state=state,
            exit_code=0,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=5000.0,
            on_output=None,
            stdout_buf=stdout,
            stderr_buf="",
        )
        assert result.status == "success"
        assert result.result == "hello world"

    def test_timeout_takes_priority_over_exit_code(self):
        """If both timed_out and exit_code are set, timeout path wins."""
        state = self._make_state(timed_out=True, had_streaming_output=False)
        result = _determine_result(
            state=state,
            exit_code=137,  # SIGKILL
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=300000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf="",
        )
        assert result.status == "error"
        assert "timed out" in (result.error or "").lower()

    def test_streaming_mode_no_session_id(self):
        """Streaming success without a session ID returns None for session."""
        state = self._make_state(new_session_id=None)
        result = _determine_result(
            state=state,
            exit_code=0,
            config_timeout=300.0,
            container_name="test-container",
            group_name="test-group",
            duration_ms=1000.0,
            on_output=AsyncMock(),
            stdout_buf="",
            stderr_buf="",
        )
        assert result.status == "success"
        assert result.new_session_id is None


# ---------------------------------------------------------------------------
# _input_to_dict edge case tests
# ---------------------------------------------------------------------------


class TestInputToDictEdgeCases:
    """Tests for _input_to_dict with various combinations of optional fields."""

    def test_minimal_input(self):
        """Only required fields, all optionals at defaults."""
        inp = ContainerInput(
            messages=[{"content": "hi"}],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=False,
        )
        d = _input_to_dict(inp)
        assert d["messages"] == [{"content": "hi"}]
        assert d["group_folder"] == "test"
        assert d["chat_jid"] == "test@g.us"
        assert d["is_admin"] is False
        # Optional fields should not be present when at defaults
        assert "session_id" not in d
        assert "is_scheduled_task" not in d
        assert "system_notices" not in d
        assert "repo_access" not in d

    def test_all_optional_fields_set(self):
        """All optional fields populated should appear in dict."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=True,
            session_id="s-1",
            is_scheduled_task=True,
            system_notices=["notice 1"],
            repo_access="owner/pynchy",
        )
        d = _input_to_dict(inp)
        assert d["session_id"] == "s-1"
        assert d["is_scheduled_task"] is True
        assert d["system_notices"] == ["notice 1"]
        assert d["repo_access"] == "owner/pynchy"

    def test_is_scheduled_task_false_omitted(self):
        """is_scheduled_task=False should NOT be included."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            is_scheduled_task=False,
        )
        d = _input_to_dict(inp)
        assert "is_scheduled_task" not in d

    def test_repo_access_none_omitted(self):
        """repo_access=None should NOT be included."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            repo_access=None,
        )
        d = _input_to_dict(inp)
        assert "repo_access" not in d

    def test_agent_core_fields_always_present(self):
        """agent_core_module and agent_core_class should always be in output."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
        )
        d = _input_to_dict(inp)
        assert "agent_core_module" in d
        assert "agent_core_class" in d

    def test_agent_core_config_included_when_set(self):
        """agent_core_config should appear when not None."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            agent_core_config={"model": "opus"},
        )
        d = _input_to_dict(inp)
        assert d["agent_core_config"] == {"model": "opus"}

    def test_agent_core_config_omitted_when_none(self):
        """agent_core_config=None should not appear in dict."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            agent_core_config=None,
        )
        d = _input_to_dict(inp)
        assert "agent_core_config" not in d
