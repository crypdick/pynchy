"""Tests for the container runner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import signal
import subprocess  # noqa: S404, RUF100 - test fixtures mock subprocess behavior and exceptions
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock, patch

import pluggy
import pytest
from conftest import make_settings
from pydantic import SecretStr

from pynchy.config import GatewayConfig
from pynchy.config.models import (
    LearningConfig,
    ObsidianLearningConfig,
    ProfileConfig,
    WorkspaceConfig,
)
from pynchy.host.container_manager.credentials import (
    _write_env_file,  # allow: private-test-imports
    shell_quote,
)
from pynchy.host.container_manager.gateway_builtin import BuiltinGateway
from pynchy.host.container_manager.ipc.write import clean_ipc_input_dir
from pynchy.host.container_manager.mounts import build_container_args, build_volume_mounts
from pynchy.host.container_manager.onecli import OneCliMaterial
from pynchy.host.container_manager.orchestrator import (
    _write_initial_input,  # allow: private-test-imports
    resolve_agent_core,
)
from pynchy.host.container_manager.serialization import input_to_dict, parse_container_output
from pynchy.host.container_manager.session_prep import (
    _sync_skills,  # allow: private-test-imports
    _write_settings_json,  # allow: private-test-imports
    is_skill_selected,
    parse_skill_tier,
)
from pynchy.host.container_manager.snapshots import write_groups_snapshot, write_tasks_snapshot
from pynchy.host.git_ops.repo import RepoContext, get_repo_token
from pynchy.host.learning.paths import LearningConfigError
from pynchy.host.orchestrator.agent_runner import (
    _build_admin_system_notices,  # allow: private-test-imports
    _build_container_input,  # allow: private-test-imports
    _PreContainerResult,  # allow: private-test-imports
    _session_tracking_output_handler,  # allow: private-test-imports
)
from pynchy.types import (
    ContainerInput,
    ContainerOutput,
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


_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


class _MockGateway(BuiltinGateway):
    """Lightweight stand-in for ``gateway.Gateway`` in credential tests.

    Subclasses the real ``BuiltinGateway`` (without calling its ``__init__``)
    so it satisfies the ``LiteLLMGateway | BuiltinGateway | None`` isinstance
    check without pulling in real gateway startup behavior.
    """

    def __init__(
        self,
        providers: set[str] | None = None,
        *,
        base_url: str = "http://host.docker.internal:4010",
    ) -> None:
        self.key = "gw-test-key"
        self._providers = providers or set()
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        return self._base_url

    def has_provider(self, name: str) -> bool:
        return name in self._providers


_SETTINGS_MODULES = [
    _CR_CREDS,
    "pynchy.host.container_manager.mounts",
    "pynchy.host.container_manager.session_prep",
    _CR_ORCH,
    "pynchy.host.container_manager.snapshots",
    "pynchy.host.learning.paths",
    "pynchy.host.learning.skills",
    "pynchy.host.orchestrator.workspace_config",
]


def _settings_overrides(
    *,
    tmp_path: Path | None,
    learning: LearningConfig | None,
    workspaces: dict[str, WorkspaceConfig] | None,
    container_timeout: float | None,
    idle_timeout: float | None,
) -> dict[str, object]:
    overrides: dict[str, object] = {
        "gateway": GatewayConfig(),
        "learning": learning or LearningConfig(),
        "workspaces": workspaces or {},
    }
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
    return overrides


def _apply_secret_overrides(
    settings,
    secret_overrides: dict[str, str] | None,
) -> None:
    if not secret_overrides:
        return
    for key, value in secret_overrides.items():
        setattr(settings.secrets, key, SecretStr(value))


def _profile_workspace(
    profile_name: str = "test-profile",
    *,
    skills: list[str] | None = None,
    model: str | None = None,
):
    profile = ProfileConfig(skills=skills or [], model=model)
    return {profile_name: profile}, WorkspaceConfig(profiles=[profile_name])


@contextlib.contextmanager
def _patch_settings(
    tmp_path: Path | None = None,
    *,
    core: str | None = None,
    container_timeout: float | None = None,
    idle_timeout: float | None = None,
    max_output_size: int | None = None,
    learning: LearningConfig | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    secret_overrides: dict[str, str] | None = None,
):
    """Patch get_settings() across all container_runner submodules."""
    overrides = _settings_overrides(
        tmp_path=tmp_path,
        learning=learning,
        workspaces=workspaces,
        container_timeout=container_timeout,
        idle_timeout=idle_timeout,
    )
    s = make_settings(**overrides)
    if core is not None:
        s.agent.default_core = core
    if max_output_size is not None:
        s.container.max_output_size = max_output_size
    _apply_secret_overrides(s, secret_overrides)
    with contextlib.ExitStack() as stack:
        for mod in _SETTINGS_MODULES:
            stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
        yield s


class FakeProcess(asyncio.subprocess.Process):
    """Simulates asyncio.subprocess.Process for testing.

    No stdin — the host writes initial input via IPC files, not a pipe.
    """

    def __init__(self) -> None:
        self.stdin = None  # stdin=DEVNULL → no pipe
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


class HangingProcess:
    """Minimal subprocess fake whose wait never completes unless killed."""

    def __init__(self) -> None:
        self.killed = False

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0

    def kill(self) -> None:
        self.killed = True


class CompletedProcess:
    """Minimal subprocess fake for commands that finish via communicate()."""

    def __init__(self, stdout: bytes = b"") -> None:
        self.stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class TestContainerProcessHelpers:
    async def test_reap_apple_runtime_orphans_signals_exact_runtime_match(self):
        """Only the runtime process for the exact Apple container UUID is reaped."""
        from pynchy.host.container_manager.process import (
            _reap_apple_runtime_orphans,  # allow: private-test-imports - Apple runtime recovery
        )

        runtime = MagicMock(cli="container")
        runtime.name = "apple"
        container_root = "/Users/me/Library/Application Support/com.apple.container/containers"
        old_container = f"{container_root}/pynchy-code-improver-old"
        ps_output = (
            b"123 /opt/homebrew/bin/container-runtime-linux start --root "
            + f"{container_root}/pynchy-code-improver --uuid pynchy-code-improver\n".encode()
            + b"456 /opt/homebrew/bin/container-runtime-linux start --root "
            + f"{old_container} --uuid pynchy-code-improver-old\n".encode()
            + b"789 /usr/bin/other --uuid pynchy-code-improver\n"
        )
        alive = {123}
        signals: list[tuple[int, signal.Signals]] = []

        def fake_kill(pid: int, sig: signal.Signals) -> None:
            if sig == 0:
                if pid in alive:
                    return
                raise ProcessLookupError
            signals.append((pid, sig))
            if sig == signal.SIGTERM:
                alive.discard(pid)

        with (
            patch("pynchy.host.container_manager.process.sys.platform", "darwin"),
            patch("pynchy.host.container_manager.process.get_runtime", return_value=runtime),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=CompletedProcess(ps_output)),
            ),
            patch("pynchy.host.container_manager.process.os.kill", side_effect=fake_kill),
        ):
            reaped = await _reap_apple_runtime_orphans("pynchy-code-improver")

        assert reaped is True
        assert signals == [(123, signal.SIGTERM)]

    async def test_force_remove_times_out_and_kills_hung_runtime_cli(self):
        """Apple Container cleanup can hang on stopped containers with orphaned runtimes."""
        from pynchy.host.container_manager.process import (
            _docker_rm_force,  # allow: private-test-imports - external cleanup side effect
        )

        proc = HangingProcess()

        with (
            patch(
                "pynchy.host.container_manager.process.get_runtime",
                return_value=MagicMock(cli="container"),
            ),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
            patch("pynchy.host.container_manager.process._RM_FORCE_TIMEOUT_SECONDS", 0.01),
            patch("pynchy.host.container_manager.process._RM_FORCE_KILL_WAIT_SECONDS", 0.01),
        ):
            await _docker_rm_force("pynchy-code-improver")

        assert proc.killed is True
        create_proc.assert_awaited_once_with(
            "container",
            "rm",
            "-f",
            "pynchy-code-improver",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def test_force_remove_reaps_apple_runtime_orphan_after_quick_return(self):
        """Apple delete can return while the stopped container runtime remains alive."""
        from pynchy.host.container_manager.process import (
            _docker_rm_force,  # allow: private-test-imports - external cleanup side effect
        )

        completed_delete = FakeProcess()
        completed_delete.close(code=1)
        retry_delete = FakeProcess()
        retry_delete.close(code=1)

        with (
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[completed_delete, retry_delete]),
            ) as create_proc,
            patch(
                "pynchy.host.container_manager.process._reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            await _docker_rm_force("pynchy-code-improver")

        reap_orphan.assert_awaited_once_with("pynchy-code-improver")
        assert create_proc.await_count == 2

    async def test_force_remove_retries_after_reaping_apple_runtime_orphan(self):
        """If Apple delete hangs, reap the orphaned runtime and retry cleanup once."""
        from pynchy.host.container_manager.process import (
            _docker_rm_force,  # allow: private-test-imports - external cleanup side effect
        )

        hung_delete = HangingProcess()
        completed_delete = FakeProcess()
        completed_delete.close(code=1)

        with (
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[hung_delete, completed_delete]),
            ) as create_proc,
            patch(
                "pynchy.host.container_manager.process._reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
            patch("pynchy.host.container_manager.process._RM_FORCE_TIMEOUT_SECONDS", 0.01),
            patch("pynchy.host.container_manager.process._RM_FORCE_KILL_WAIT_SECONDS", 0.01),
        ):
            await _docker_rm_force("pynchy-code-improver")

        assert hung_delete.killed is True
        reap_orphan.assert_awaited_once_with("pynchy-code-improver")
        assert create_proc.await_count == 2


class TestInputSerialization:
    def test_basic_fields_snake_case(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="my-group",
            chat_jid="chat@g.us",
            is_admin=True,
        )
        d = input_to_dict(inp)
        assert d == {
            "messages": [{"message_type": "user", "content": "hi"}],
            "group_folder": "my-group",
            "chat_jid": "chat@g.us",
            "is_admin": True,
            "is_scheduled_task": False,
            "invocation_ts": 0.0,
            "agent_core_module": "agent_runner.cores.openai",
            "agent_core_class": "OpenAIAgentCore",
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
        d = input_to_dict(inp)
        assert d["session_id"] == "sess-1"
        assert d["is_scheduled_task"] is True

    def test_optional_fields_omitted_when_none(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
        d = input_to_dict(inp)
        assert "session_id" not in d  # None → omitted
        assert d["is_scheduled_task"] is False  # False → included (non-None)


class TestWriteInitialInput:
    """Tests for _write_initial_input — atomic file write of ContainerInput."""

    def test_creates_initial_json_with_correct_content(self, tmp_path: Path):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hello"}],
            group_folder="test-group",
            chat_jid="chat@g.us",
            is_admin=False,
        )
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        _write_initial_input(inp, input_dir)

        filepath = input_dir / "initial.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert data["messages"] == [{"message_type": "user", "content": "hello"}]
        assert data["group_folder"] == "test-group"
        assert data["chat_jid"] == "chat@g.us"
        assert data["is_admin"] is False

    def test_creates_parent_directories(self, tmp_path: Path):
        inp = ContainerInput(
            messages=[],
            group_folder="deep-group",
            chat_jid="c",
            is_admin=False,
        )
        input_dir = tmp_path / "a" / "b" / "c" / "input"
        assert not input_dir.exists()
        _write_initial_input(inp, input_dir)
        assert (input_dir / "initial.json").exists()

    def test_atomic_write_no_tmp_left_behind(self, tmp_path: Path):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
        input_dir = tmp_path / "input"
        _write_initial_input(inp, input_dir)

        # Only initial.json should exist — no .tmp file
        files = list(input_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == "initial.json"

    def test_overwrites_existing_initial_json(self, tmp_path: Path):
        """Verify idempotency — a second call replaces the first file."""
        input_dir = tmp_path / "input"
        inp1 = ContainerInput(
            messages=[{"message_type": "user", "content": "first"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
        inp2 = ContainerInput(
            messages=[{"message_type": "user", "content": "second"}],
            group_folder="g",
            chat_jid="c",
            is_admin=True,
        )
        _write_initial_input(inp1, input_dir)
        _write_initial_input(inp2, input_dir)

        data = json.loads((input_dir / "initial.json").read_text())
        assert data["messages"][0]["content"] == "second"
        assert data["is_admin"] is True

    def test_optional_fields_round_trip(self, tmp_path: Path):
        """Optional fields like session_id survive the write/read cycle."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
            session_id="sess-42",
            is_scheduled_task=True,
            system_notices=["notice1"],
        )
        input_dir = tmp_path / "input"
        _write_initial_input(inp, input_dir)

        data = json.loads((input_dir / "initial.json").read_text())
        assert data["session_id"] == "sess-42"
        assert data["is_scheduled_task"] is True
        assert data["system_notices"] == ["notice1"]


class TestCleanIpcInputDir:
    """clean_ipc_input_dir should respect preserve_initial flag."""

    def test_preserves_initial_json(self, tmp_path: Path) -> None:
        settings = make_settings(data_dir=tmp_path)
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "initial.json").write_text('{"messages": []}')
        (input_dir / "stale-msg.json").write_text('{"type": "message"}')
        (input_dir / "_close").write_text("")

        with patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings):
            clean_ipc_input_dir("test-group", preserve_initial=True)

        assert (input_dir / "initial.json").exists()
        assert not (input_dir / "stale-msg.json").exists()
        assert not (input_dir / "_close").exists()

    def test_deletes_everything_when_not_preserving(self, tmp_path: Path) -> None:
        settings = make_settings(data_dir=tmp_path)
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "initial.json").write_text('{"messages": []}')
        (input_dir / "stale-msg.json").write_text('{"type": "message"}')
        (input_dir / "_close").write_text("")

        with patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings):
            clean_ipc_input_dir("test-group", preserve_initial=False)

        assert not (input_dir / "initial.json").exists()
        assert not (input_dir / "stale-msg.json").exists()
        assert not (input_dir / "_close").exists()

    def test_noop_for_none_group(self) -> None:
        """Should not raise when group_folder is None."""
        clean_ipc_input_dir(None)


class TestOutputParsing:
    def test_parses_snake_case_json(self):
        out = parse_container_output(
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
        out = parse_container_output(json.dumps({"status": "error", "error": "boom"}))
        assert out.status == "error"
        assert out.error == "boom"
        assert out.result is None


class TestContainerArgs:
    def test_readonly_uses_mount_flag(self):
        mounts = [VolumeMount("/host/path", "/container/path", readonly=True)]
        args = build_container_args(mounts, "test-container")
        assert "--mount" in args
        assert any("readonly" in a for a in args)
        assert "-v" not in args[args.index("--mount") :]  # no -v after --mount for this mount

    def test_readwrite_uses_v_flag(self):
        mounts = [VolumeMount("/host/path", "/container/path", readonly=False)]
        args = build_container_args(mounts, "test-container")
        assert "-v" in args
        assert "/host/path:/container/path" in args

    def test_apple_readonly_file_mount_uses_volume_flag(self, tmp_path: Path):
        host_file = tmp_path / "onecli-ca.pem"
        host_file.write_text("ca")
        ca_container_path = str(PurePosixPath("/", "tmp", "onecli-ca.pem"))
        mounts = [VolumeMount(str(host_file), ca_container_path, readonly=True)]
        runtime = MagicMock(name="runtime")
        runtime.name = "apple"

        with patch("pynchy.plugins.runtimes.detection.get_runtime", return_value=runtime):
            args = build_container_args(mounts, "test-container")

        assert "-v" in args
        assert f"{host_file}:{ca_container_path}:ro" in args
        assert "--mount" not in args

    def test_includes_name_and_image(self):
        args = build_container_args([], "my-container")
        assert args[:3] == ["run", "--name", "my-container"]
        # Last arg is the image
        assert args[-1].endswith("-agent:latest")


# ---------------------------------------------------------------------------
# Mount building tests (require tmp dirs)
# ---------------------------------------------------------------------------


class TestMountBuilding:
    def test_learning_disabled_does_not_add_vault_mount(self, tmp_path: Path):
        with _patch_settings(tmp_path, learning=LearningConfig(enabled=False)):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        assert all(m.container_path != "/workspace/vault" for m in mounts)

    def test_learning_enabled_mounts_vault_readwrite(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(
                vault_root=str(vault),
                mount_path="/mnt/obsidian",
            ),
        )

        with _patch_settings(tmp_path, learning=learning):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        vault_mount = next(
            (m for m in mounts if m.container_path == "/mnt/obsidian"),
            None,
        )
        assert vault_mount is not None, "expected vault mount"
        assert vault_mount.host_path == str(vault.resolve())
        assert vault_mount.readonly is False

    def test_learning_mount_creates_profile_fallback_dirs(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!")
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        profile_root = vault.resolve() / "systems/pynchy/profiles/deep-work"
        assert (profile_root / "memory").is_dir()
        assert (profile_root / "skills").is_dir()

    def test_learning_mount_does_not_scan_skills_when_workspace_skills_is_none(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )

        with (
            _patch_settings(tmp_path, learning=learning),
            patch(
                "pynchy.host.container_manager.mounts.iter_learned_skill_dirs",
                side_effect=AssertionError("unexpected scan"),
            ),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        assert any(m.container_path == "/workspace/vault" for m in mounts)

    def test_learning_mount_does_not_scan_skills_when_workspace_skills_is_empty(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=[])
        workspaces = {"test-group": workspace}

        with (
            _patch_settings(tmp_path, learning=learning, workspaces=workspaces) as settings,
            patch(
                "pynchy.host.container_manager.mounts.iter_learned_skill_dirs",
                side_effect=AssertionError("unexpected scan"),
            ),
        ):
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        assert any(m.container_path == "/workspace/vault" for m in mounts)

    def test_learning_mount_syncs_vault_profile_skill_when_learned_selected(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        learned_skill = vault / "systems/pynchy/profiles/deep-work/skills/remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=["learned"])
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        skill_dst = tmp_path / "data/sessions/test-group/.claude/skills/remember-routing/SKILL.md"
        assert skill_dst.exists()

    def test_codex_home_receives_selected_plugin_skills(self, tmp_path: Path):
        plugin_skill = tmp_path / "vault-skills" / "calendar-caldav"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text(
            "---\nname: calendar-caldav\ntier: community\n---\n# Calendar\n"
        )

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        profiles, workspace = _profile_workspace(skills=["calendar-caldav"])
        workspaces = {"test-group": workspace}
        with _patch_settings(tmp_path, workspaces=workspaces) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False, plugin_manager=FakePM())

        claude_skill = tmp_path / "data/sessions/test-group/.claude/skills/calendar-caldav/SKILL.md"
        codex_skill = tmp_path / "data/sessions/test-group/.codex/skills/calendar-caldav/SKILL.md"
        assert claude_skill.exists()
        assert codex_skill.read_text() == claude_skill.read_text()

    @pytest.mark.parametrize("vault_state", ["missing", "file"])
    def test_learning_enabled_requires_existing_vault_directory(
        self,
        tmp_path: Path,
        vault_state: str,
    ):
        vault = tmp_path / "vault"
        if vault_state == "file":
            vault.write_text("not a directory")
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )

        (tmp_path / "groups" / "test-group").mkdir(parents=True)
        with (
            _patch_settings(tmp_path, learning=learning),
            pytest.raises(LearningConfigError, match=r"vault_root.*directory"),
        ):
            build_volume_mounts(TEST_GROUP, is_admin=False)

    def test_admin_group_has_repo_mount(self, tmp_path: Path):
        worktree_path = tmp_path / "worktrees" / "admin-1"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            paths = [m.container_path for m in mounts]
            assert "/workspace/repos/owner/pynchy" in paths
            assert "/workspace/group" in paths
            assert "/workspace/global" not in paths

    def test_nonadmin_group_has_no_global_mount(self, tmp_path: Path):
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
            mounts = build_volume_mounts(group, is_admin=False)

            paths = [m.container_path for m in mounts]
            assert "/workspace/repos/owner/pynchy" not in paths
            assert "/workspace/group" in paths
            assert "/workspace/global" not in paths

    def test_nonadmin_repo_access_uses_worktree_path(self, tmp_path: Path):
        """Non-admin group with repo access mounts the worktree under /workspace/repos."""
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
            mounts = build_volume_mounts(
                group, is_admin=False, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            repo_mount = next(
                m for m in mounts if m.container_path == "/workspace/repos/owner/pynchy"
            )
            assert repo_mount.host_path == str(worktree_path)
            assert repo_mount.readonly is False

            # .git dir mounted at host path so worktree gitdir reference resolves
            git_mount = next(m for m in mounts if m.host_path == str(tmp_path / ".git"))
            assert git_mount.container_path == str(tmp_path / ".git")

    def test_admin_uses_worktree(self, tmp_path: Path):
        """Admin group uses worktree just like any other repo_access group."""
        worktree_path = tmp_path / "worktrees" / "admin-1"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            repo_mount = next(
                m for m in mounts if m.container_path == "/workspace/repos/owner/pynchy"
            )
            assert repo_mount.host_path == str(worktree_path)
            assert repo_mount.readonly is False

    def test_admin_gets_raw_host_repo_mount(self, tmp_path: Path):
        """Admin group gets a raw host repo mount when repo_ctx is provided."""
        worktree_path = tmp_path / "worktrees" / "admin-1"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            raw_mount = next(
                m for m in mounts if m.container_path == "/danger/raw-host-repos/owner/pynchy"
            )
            assert raw_mount.host_path == str(tmp_path)
            assert raw_mount.readonly is False

    def test_repo_mounts_support_multiple_repos(self, tmp_path: Path):
        repo_a = RepoContext(
            slug="owner/pynchy", root=tmp_path / "repo-a", worktrees_dir=tmp_path / "wt-a"
        )
        repo_b = RepoContext(
            slug="owner/tools", root=tmp_path / "repo-b", worktrees_dir=tmp_path / "wt-b"
        )
        wt_a = tmp_path / "worktrees" / "pynchy"
        wt_b = tmp_path / "worktrees" / "tools"
        for path in (repo_a.root / ".git", repo_b.root / ".git", wt_a, wt_b):
            path.mkdir(parents=True)

        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "multi").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="multi@g.us",
                name="Multi",
                folder="multi",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group,
                is_admin=False,
                repo_mounts=[(repo_a, wt_a), (repo_b, wt_b)],
            )

        by_container = {m.container_path: m.host_path for m in mounts}
        assert by_container["/workspace/repos/owner/pynchy"] == str(wt_a)
        assert by_container["/workspace/repos/owner/tools"] == str(wt_b)

    def test_nonadmin_does_not_get_raw_host_repo_mount(self, tmp_path: Path):
        """Non-admin groups never get the raw host repo mount."""
        worktree_path = tmp_path / "worktrees" / "other"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "other").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="other@g.us",
                name="Other",
                folder="other",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=False, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            paths = [m.container_path for m in mounts]
            assert "/danger/raw-host-repos/owner/pynchy" not in paths

    def test_admin_no_config_toml_when_missing(self, tmp_path: Path):
        """Admin group doesn't get config.toml mount if the file doesn't exist."""
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(group, is_admin=True)

            paths = [m.container_path for m in mounts]
            assert "/workspace/repos/owner/pynchy/config.toml" not in paths

    def test_onecli_material_adds_mounts_env_and_suppresses_gh_token(
        self,
        tmp_path: Path,
    ):
        """OneCLI material is mounted and raw GitHub tokens stay out of env files."""
        ca_host_path = tmp_path / "onecli-ca.pem"
        ca_container_path = str(PurePosixPath("/", "tmp", "onecli-ca.pem"))
        material = OneCliMaterial(
            env_vars={
                "HTTPS_PROXY": "http://proxy",
                "SSL_CERT_FILE": ca_container_path,
            },
            mounts=[VolumeMount(str(ca_host_path), ca_container_path, readonly=True)],
            warnings=[],
        )
        with (
            _patch_settings(tmp_path, secret_overrides={"gh_token": "explicit-token"}),
            patch(
                "pynchy.host.container_manager.mounts.prepare_onecli_material",
                return_value=material,
                create=True,
            ),
            patch(f"{_GATEWAY}.get_gateway", return_value=None),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(group, is_admin=True)

        assert any(m.container_path == ca_container_path for m in mounts)
        env_file = tmp_path / "data" / "env" / "admin-1" / "env"
        content = env_file.read_text()
        assert "HTTPS_PROXY='http://proxy'" in content
        assert f"SSL_CERT_FILE='{ca_container_path}'" in content
        assert "GH_TOKEN" not in content


# ---------------------------------------------------------------------------
# Credential / env file tests
# ---------------------------------------------------------------------------


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

    def test_gateway_host_bypasses_container_proxy_vars(self, tmp_path: Path):
        """Local gateway traffic must not route through OneCLI's env proxy."""
        gw = _MockGateway(providers={"openai"}, base_url="http://192.168.64.1:4000")
        proxy_env = {
            "HTTP_PROXY": "http://proxy.internal:8080",
            "HTTPS_PROXY": "http://proxy.internal:8080",
            "NO_PROXY": "metadata.internal",
        }
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}._read_gh_token", return_value=None),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            env_dir = _write_env_file(
                is_admin=True,
                group_folder="test",
                extra_env_vars=proxy_env,
            )

        assert env_dir is not None
        content = (env_dir / "env").read_text()
        expected_hosts = (
            "metadata.internal,localhost,127.0.0.1,::1,host.docker.internal,192.168.64.1"
        )
        assert "HTTP_PROXY='http://proxy.internal:8080'" in content
        assert f"NO_PROXY='{expected_hosts}'" in content
        assert f"no_proxy='{expected_hosts}'" in content

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

    def test_auto_discovers_gh_token_for_admin(self, tmp_path: Path):
        """GH_TOKEN is auto-discovered from gh CLI for admin containers."""
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

    def test_non_admin_excludes_gh_token(self, tmp_path: Path):
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
            admin_dir = _write_env_file(is_admin=True, group_folder="admin-group")
            nonadmin_dir = _write_env_file(is_admin=False, group_folder="other-group")
            assert admin_dir != nonadmin_dir
            assert "GH_TOKEN" in (admin_dir / "env").read_text()
            assert "GH_TOKEN" not in (nonadmin_dir / "env").read_text()

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
    """gh-CLI token discovery, driven through the public get_repo_token().

    With no per-repo token and no configured gh_token secret, get_repo_token()
    falls through to the gh CLI, so these exercise that discovery path.
    """

    def test_returns_token_from_gh_cli(self):
        mock_result = type("Result", (), {"returncode": 0, "stdout": "gho_test123\n"})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            assert get_repo_token("owner/repo") == "gho_test123"

    def test_returns_none_on_failure(self):
        mock_result = type("Result", (), {"returncode": 1, "stdout": ""})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            assert get_repo_token("owner/repo") is None

    def test_returns_none_when_gh_not_installed(self):
        with patch(f"{_CR_CREDS}.subprocess.run", side_effect=FileNotFoundError):
            assert get_repo_token("owner/repo") is None

    def test_returns_none_on_timeout(self):
        with patch(
            f"{_CR_CREDS}.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 5),
        ):
            assert get_repo_token("owner/repo") is None


class TestReadGitIdentity:
    """Git identity discovery, observed via the env file _write_env_file writes.

    A gateway provider is present so the env file is written; git config is
    faked via subprocess.run so the GIT_* vars reflect the discovered identity.
    """

    def test_returns_name_and_email(self, tmp_path: Path):
        def mock_run(cmd, **kwargs):
            key = cmd[-1]
            if key == "user.name":
                return type("R", (), {"returncode": 0, "stdout": "Alice\n"})()
            if key == "user.email":
                return type("R", (), {"returncode": 0, "stdout": "alice@test.com\n"})()
            return type("R", (), {"returncode": 1, "stdout": ""})()

        gw = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}.subprocess.run", side_effect=mock_run),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GIT_AUTHOR_NAME='Alice'" in content
            assert "GIT_COMMITTER_NAME='Alice'" in content
            assert "GIT_AUTHOR_EMAIL='alice@test.com'" in content
            assert "GIT_COMMITTER_EMAIL='alice@test.com'" in content

    def test_returns_none_when_not_configured(self, tmp_path: Path):
        mock_result = type("R", (), {"returncode": 1, "stdout": ""})()
        gw = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GIT_AUTHOR_NAME" not in content
            assert "GIT_AUTHOR_EMAIL" not in content

    def test_returns_partial_when_only_name_set(self, tmp_path: Path):
        def mock_run(cmd, **kwargs):
            if cmd[-1] == "user.name":
                return type("R", (), {"returncode": 0, "stdout": "Bob\n"})()
            return type("R", (), {"returncode": 1, "stdout": ""})()

        gw = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gw),
            patch(f"{_CR_CREDS}.subprocess.run", side_effect=mock_run),
        ):
            env_dir = _write_env_file(is_admin=True, group_folder="test")
            assert env_dir is not None
            content = (env_dir / "env").read_text()
            assert "GIT_AUTHOR_NAME='Bob'" in content
            assert "GIT_AUTHOR_EMAIL" not in content


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestTasksSnapshot:
    def test_admin_sees_all_tasks(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [
                {"groupFolder": "admin-1", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot("admin-1", tasks, is_admin=True)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "admin-1" / "current_tasks.json").read_text()
            )
            assert len(result) == 2

    def test_nonadmin_sees_only_own_tasks(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [
                {"groupFolder": "admin-1", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot("other", tasks, is_admin=False)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "current_tasks.json").read_text()
            )
            assert len(result) == 1
            assert result[0]["id"] == "t2"

    def test_admin_includes_host_jobs(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [{"groupFolder": "admin-1", "id": "t1"}]
            host_jobs = [{"type": "host", "id": "h1", "name": "daily-backup"}]
            write_tasks_snapshot("admin-1", tasks, is_admin=True, host_jobs=host_jobs)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "admin-1" / "current_tasks.json").read_text()
            )
            assert len(result) == 2
            assert result[0]["id"] == "t1"
            assert result[1]["id"] == "h1"
            assert result[1]["type"] == "host"

    def test_nonadmin_ignores_host_jobs(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [{"groupFolder": "other", "id": "t1"}]
            host_jobs = [{"type": "host", "id": "h1", "name": "daily-backup"}]
            write_tasks_snapshot("other", tasks, is_admin=False, host_jobs=host_jobs)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "current_tasks.json").read_text()
            )
            assert len(result) == 1
            assert result[0]["id"] == "t1"


class TestGroupsSnapshot:
    def test_admin_sees_all_groups(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            groups = [{"jid": "a@g.us"}, {"jid": "b@g.us"}]
            write_groups_snapshot("admin-1", groups, {"a@g.us", "b@g.us"}, is_admin=True)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "admin-1" / "available_groups.json").read_text()
            )
            assert len(result["groups"]) == 2

    def test_nonadmin_sees_no_groups(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            groups = [{"jid": "a@g.us"}]
            write_groups_snapshot("other", groups, {"a@g.us"}, is_admin=False)
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
        """Covers the `if plugin_manager:` guard for the None case."""
        module, cls = resolve_agent_core(None)
        assert module == "agent_runner.cores.openai"
        assert cls == "OpenAIAgentCore"

    def test_returns_defaults_when_no_cores_registered(self):
        """Plugin manager exists but no agent core plugins are installed."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return []

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        module, cls = resolve_agent_core(FakePM())
        assert module == "agent_runner.cores.openai"
        assert cls == "OpenAIAgentCore"

    def test_uses_matching_core_by_name(self):
        """When a core matches DEFAULT_AGENT_CORE, use it."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    {"name": "openai", "module": "cores.openai", "class_name": "OpenAICore"},
                    {"name": "claude", "module": "cores.claude_v2", "class_name": "ClaudeV2Core"},
                ]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

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

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

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

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(core="custom"):
            module, cls = resolve_agent_core(FakePM())

        assert module == "cores.custom"
        assert cls == "CustomCore"


# ---------------------------------------------------------------------------
# Container input core config
# ---------------------------------------------------------------------------


class TestContainerInputAgentCoreConfig:
    """Test model configuration passed from host settings into agent cores."""

    @staticmethod
    def _ctx() -> _PreContainerResult:
        return _PreContainerResult(
            is_admin=False,
            repo_access=None,
            repo_accesses=[],
            system_prompt_append=None,
            session_id=None,
            system_notices=[],
            agent_core_module="agent_runner.cores.codex",
            agent_core_class="CodexCLIAgentCore",
            wrapped_on_output=AsyncMock(),
            config_timeout=30.0,
            snapshot_ms=0.0,
        )

    def test_agent_model_settings_flow_to_core_config(self):
        from pynchy.config import AgentConfig

        settings = make_settings(
            agent=AgentConfig(
                model="chatgpt/gpt-5.3-codex",
            )
        )

        with patch("pynchy.host.orchestrator.agent_runner.get_settings", return_value=settings):
            result = _build_container_input([], self._ctx(), "chat", TEST_GROUP)

        assert result.agent_core_config == {"model": "chatgpt/gpt-5.3-codex"}

    def test_default_agent_model_flows_to_core_config(self):
        settings = make_settings()

        with patch("pynchy.host.orchestrator.agent_runner.get_settings", return_value=settings):
            result = _build_container_input([], self._ctx(), "chat", TEST_GROUP)

        assert result.agent_core_config is None

    def test_workspace_model_overrides_global_agent_model(self):
        from pynchy.config import AgentConfig

        profiles, workspace = _profile_workspace(
            "codex-workspace",
            model="chatgpt/gpt-5.3-codex-spark",
        )
        settings = make_settings(
            agent=AgentConfig(model="chatgpt/gpt-5.3-codex"),
            profiles=profiles,
            workspaces={TEST_GROUP.folder: workspace},
        )

        with (
            patch("pynchy.host.orchestrator.agent_runner.get_settings", return_value=settings),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
        ):
            result = _build_container_input([], self._ctx(), "chat", TEST_GROUP)

        assert result.agent_core_config == {"model": "chatgpt/gpt-5.3-codex-spark"}

    def test_workspace_model_override_replaces_global_model(self):
        from pynchy.config import AgentConfig

        profiles, workspace = _profile_workspace(
            "codex-workspace",
            model="chatgpt/gpt-5.3-codex-spark",
        )
        settings = make_settings(
            agent=AgentConfig(model="chatgpt/gpt-5.3-codex"),
            profiles=profiles,
            workspaces={TEST_GROUP.folder: workspace},
        )

        with (
            patch("pynchy.host.orchestrator.agent_runner.get_settings", return_value=settings),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
        ):
            result = _build_container_input([], self._ctx(), "chat", TEST_GROUP)

        assert result.agent_core_config == {"model": "chatgpt/gpt-5.3-codex-spark"}


class TestAgentRunnerPreContainerHelpers:
    @pytest.mark.asyncio
    async def test_session_tracking_output_handler_records_session(self):
        class _Deps:
            def __init__(self) -> None:
                self.sessions: dict[str, str] = {}
                self._session_cleared: set[str] = set()
                self.workspaces: dict[str, WorkspaceProfile] = {}
                self.queue = MagicMock()
                self.plugin_manager = None

            async def get_available_groups(self) -> list[dict[str, object]]:
                return []

            async def broadcast_agent_input(
                self,
                chat_jid: str,
                messages: list[dict[str, object]],
                *,
                source: str = "user",
            ) -> None:
                return None

        deps = _Deps()
        on_output = AsyncMock()
        output = ContainerOutput(
            status="success",
            type="system",
            system_subtype="thread.started",
            system_data={"session_id": "codex:thread-1"},
        )

        with patch(
            "pynchy.host.orchestrator._agent_runner_preflight.set_session",
            new_callable=AsyncMock,
        ) as persist:
            handler = _session_tracking_output_handler(deps, "test-group", on_output)
            await handler(output)

        assert deps.sessions == {"test-group": "codex:thread-1"}
        persist.assert_awaited_once()
        on_output.assert_awaited_once_with(output)

    def test_build_admin_system_notices_includes_repo_warnings_and_guidance(self):
        repo_ctx = MagicMock()
        repo_ctx.worktrees_dir = Path.cwd() / "worktrees"

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_repo_context",
                return_value=repo_ctx,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.is_repo_dirty",
                return_value=True,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.count_unpushed_commits",
                return_value=2,
            ),
        ):
            notices = _build_admin_system_notices(
                "test-group",
                is_admin=True,
                repo_access="owner/repo",
            )

        assert any("uncommitted local changes" in notice for notice in notices)
        assert any("haven't been pushed" in notice for notice in notices)
        assert notices[-1].startswith("Consider whether to address")


# ---------------------------------------------------------------------------
# _sync_skills tests
# ---------------------------------------------------------------------------


class TestSyncSkills:
    """Test skill syncing from built-in skills and plugin skills into session dir."""

    def test_copies_builtin_skills(self, tmp_path: Path):
        """Built-in skills are copied to the session .claude/skills/ dir."""
        # Create a built-in skill
        builtin_skill = tmp_path / "src" / "pynchy" / "agent" / "skills" / "my-skill"
        builtin_skill.mkdir(parents=True)
        (builtin_skill / "skill.md").write_text("# My Skill\nDo stuff.")
        (builtin_skill / "config.json").write_text('{"name": "my-skill"}')

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["*"])

        skills_dst = session_dir / "skills" / "my-skill"
        assert skills_dst.exists()
        assert (skills_dst / "skill.md").read_text() == "# My Skill\nDo stuff."
        assert (skills_dst / "config.json").exists()

    def test_no_skills_dir_is_safe(self, tmp_path: Path):
        """Missing agent/skills/ dir should not crash."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir)

        # skills/ directory should still be created (empty)
        assert (session_dir / "skills").exists()

    def test_syncs_generated_onecli_gateway_skill(self, tmp_path: Path):
        """Session skill sync delegates generated OneCLI gateway skill installation."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with (
            _patch_settings(tmp_path),
            patch("pynchy.host.container_manager.session_prep.sync_onecli_gateway_skill") as sync,
        ):
            _sync_skills(session_dir)

        sync.assert_called_once_with(session_dir / "skills")

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

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, plugin_manager=FakePM(), workspace_skills=["*"])

        ext_dst = session_dir / "skills" / "ext-skill"
        assert ext_dst.exists()
        assert (ext_dst / "skill.md").read_text() == "# External Skill"

    def test_bad_plugin_skill_path_does_not_block_later_plugin_skill(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        """One malformed plugin path should not prevent later plugin skills from syncing."""
        plugin_skill = tmp_path / "plugins" / "ext-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "skill.md").write_text("# External Skill")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[None, str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        caplog.set_level(logging.ERROR)
        with _patch_settings(tmp_path):
            _sync_skills(session_dir, plugin_manager=FakePM(), workspace_skills=["*"])

        ext_dst = session_dir / "skills" / "ext-skill"
        assert ext_dst.exists()
        assert "Failed to sync plugin skill" in caplog.text

    def test_plugin_skill_name_collision_raises(self, tmp_path: Path):
        """Plugin skill that shadows a built-in skill raises ValueError."""
        # Create built-in skill
        builtin_skill = tmp_path / "src" / "pynchy" / "agent" / "skills" / "my-skill"
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

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with (
            _patch_settings(tmp_path),
            pytest.raises(ValueError, match="collision"),
        ):
            _sync_skills(session_dir, plugin_manager=FakePM(), workspace_skills=["*"])

    def test_skips_nonexistent_plugin_skill_path(self, tmp_path: Path):
        """Plugin skill paths that don't exist are skipped with a warning."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(tmp_path / "nonexistent-skill")]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            # Should not crash
            _sync_skills(session_dir, plugin_manager=FakePM())

    def test_ignores_files_in_skills_dir(self, tmp_path: Path):
        """Files (not directories) in agent/skills/ are ignored."""
        skills_dir = tmp_path / "src" / "pynchy" / "agent" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "README.md").write_text("not a skill dir")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir)

        # Only the skills/ directory should exist, no README.md copied
        assert not (session_dir / "skills" / "README.md").exists()

    def test_learned_skills_are_synced_when_learned_tier_selected(self, tmp_path: Path):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        (learned_skill / "notes.md").write_text("Use the right queue.")
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )

        skill_dst = session_dir / "skills" / "remember-routing"
        assert (skill_dst / "SKILL.md").exists()
        assert (skill_dst / "notes.md").read_text() == "Use the right queue."

    def test_learned_skills_are_synced_when_all_skills_selected(self, tmp_path: Path):
        learned_skill = tmp_path / "vault-skills" / "obsidian-filer"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: obsidian-filer\ntier: learned\n---\n# Obsidian Filer\n"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["*"],
                learned_skill_paths=[learned_skill],
            )

        assert (session_dir / "skills" / "obsidian-filer" / "SKILL.md").exists()

    def test_learned_skills_are_not_synced_when_workspace_skills_is_none(
        self,
        tmp_path: Path,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=None,
                learned_skill_paths=[learned_skill],
            )

        assert not (session_dir / "skills" / "remember-routing").exists()

    @pytest.mark.parametrize(
        "frontmatter",
        [
            "---\nname: remember-routing\n---\n# Remember Routing\n",
            "---\nname: remember-routing\ntier: community\n---\n# Remember Routing\n",
        ],
    )
    def test_learned_skill_source_tier_does_not_select_from_matching_nonlearned_tier(
        self,
        tmp_path: Path,
        frontmatter: str,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(frontmatter)
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["community"],
                learned_skill_paths=[learned_skill],
            )

        assert not (session_dir / "skills" / "remember-routing").exists()

    @pytest.mark.parametrize(
        "frontmatter",
        [
            "---\nname: remember-routing\n---\n# Remember Routing\n",
            "---\nname: remember-routing\ntier: community\n---\n# Remember Routing\n",
        ],
    )
    def test_learned_skill_source_tier_is_normalized_for_learned_selection(
        self,
        tmp_path: Path,
        frontmatter: str,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(frontmatter)
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )

        assert (session_dir / "skills" / "remember-routing" / "SKILL.md").exists()

    def test_learned_skill_name_alone_does_not_select_learned_namespace(
        self,
        tmp_path: Path,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: ops\n---\n# Remember Routing\n"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["remember-routing"],
                learned_skill_paths=[learned_skill],
            )

        assert not (session_dir / "skills" / "remember-routing").exists()

    def test_learned_skill_collision_is_skipped_and_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        builtin_skill = tmp_path / "src" / "pynchy" / "agent" / "skills" / "shared-name"
        builtin_skill.mkdir(parents=True)
        (builtin_skill / "SKILL.md").write_text("built-in")

        learned_skill = tmp_path / "vault-skills" / "shared-name"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: shared-name\ntier: learned\n---\nlearned"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        caplog.set_level(logging.WARNING)
        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["*"],
                learned_skill_paths=[learned_skill],
            )

        copied_skill = session_dir / "skills" / "shared-name" / "SKILL.md"
        assert copied_skill.read_text() == "built-in"
        assert "Skipping learned skill" in caplog.text
        assert "collision" in caplog.text

    def test_learned_skill_resync_updates_prior_learned_copy(self, tmp_path: Path):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        notes = learned_skill / "notes.md"
        notes.write_text("first version")
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )
            notes.write_text("second version")
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )

        skill_dst = session_dir / "skills" / "remember-routing"
        assert (skill_dst / "notes.md").read_text() == "second version"

    def test_learned_skill_removed_from_vault_prunes_prior_managed_copy(
        self,
        tmp_path: Path,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )
            shutil.rmtree(learned_skill)
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[],
            )

        assert not (session_dir / "skills" / "remember-routing").exists()

    @pytest.mark.parametrize("workspace_skills", [None, ["core"]])
    def test_deselected_learned_skills_prune_prior_managed_copy(
        self,
        tmp_path: Path,
        workspace_skills: list[str] | None,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )
            _sync_skills(
                session_dir,
                workspace_skills=workspace_skills,
                learned_skill_paths=[learned_skill],
            )

        assert not (session_dir / "skills" / "remember-routing").exists()

    def test_symlinked_marker_destination_is_not_pruned_or_overwritten(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        session_dir = tmp_path / "session" / ".claude"
        skills_dst = session_dir / "skills"
        skills_dst.mkdir(parents=True)
        target = tmp_path / "external-managed-skill"
        target.mkdir()
        (target / ".pynchy-learned-skill").write_text("managed by pynchy\n")
        (target / "payload.md").write_text("external content")
        symlink_dst = skills_dst / "remember-routing"
        symlink_dst.symlink_to(target, target_is_directory=True)

        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        (learned_skill / "payload.md").write_text("vault content")

        caplog.set_level(logging.WARNING)
        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[],
            )
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )

        assert symlink_dst.is_symlink()
        assert symlink_dst.resolve() == target.resolve()
        assert (target / "payload.md").read_text() == "external content"
        assert "collision" in caplog.text

    def test_learned_skill_copy_failure_is_skipped_and_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        learned_skill = tmp_path / "vault-skills" / "remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        (learned_skill / "notes.md").write_text("Use the right queue.")
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        caplog.set_level(logging.WARNING)
        with (
            _patch_settings(tmp_path),
            patch(
                "pynchy.host.container_manager.session_prep.shutil.copy2",
                side_effect=OSError("copy denied"),
            ),
        ):
            _sync_skills(
                session_dir,
                workspace_skills=["learned"],
                learned_skill_paths=[learned_skill],
            )

        assert not (session_dir / "skills" / "remember-routing").exists()
        assert "Skipping learned skill" in caplog.text
        assert "copy denied" in caplog.text

    def test_learned_skill_collision_with_plugin_is_skipped_and_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        plugin_skill = tmp_path / "plugins" / "shared-name"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text("plugin")

        learned_skill = tmp_path / "vault-skills" / "shared-name"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: shared-name\ntier: learned\n---\nlearned"
        )
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        caplog.set_level(logging.WARNING)
        with _patch_settings(tmp_path):
            _sync_skills(
                session_dir,
                plugin_manager=FakePM(),
                workspace_skills=["*"],
                learned_skill_paths=[learned_skill],
            )

        copied_skill = session_dir / "skills" / "shared-name" / "SKILL.md"
        assert copied_skill.read_text() == "plugin"
        assert "Skipping learned skill" in caplog.text
        assert "collision" in caplog.text


# ---------------------------------------------------------------------------
# Skill tier helpers
# ---------------------------------------------------------------------------


class TestParseSkillTier:
    """Test SKILL.md frontmatter parsing for name and tier."""

    def test_valid_frontmatter(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ntier: core\n---\n# My Skill\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "core"

    def test_missing_tier_defaults_to_community(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_no_skill_md_defaults(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_no_frontmatter_delimiters(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Just a heading\nNo frontmatter here.\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_dev_tier(self, tmp_path: Path):
        skill_dir = tmp_path / "code-improver"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: code-improver\ntier: dev\n---\n# Code Improver\n"
        )
        name, tier = parse_skill_tier(skill_dir)
        assert name == "code-improver"
        assert tier == "dev"

    def test_name_defaults_to_dir_name(self, tmp_path: Path):
        """When name is missing from frontmatter, use directory name."""
        skill_dir = tmp_path / "web-search"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\ntier: core\n---\n# Web Search\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "web-search"
        assert tier == "core"


class TestIsSkillSelected:
    """Test skill selection resolution logic."""

    def test_none_is_core_only(self):
        """skills=None means core-only (safe default)."""
        assert is_skill_selected("any-skill", "community", None) is False
        assert is_skill_selected("browser", "core", None) is True

    def test_star_includes_everything(self):
        assert is_skill_selected("any-skill", "community", ["*"]) is True

    def test_tier_match(self):
        assert is_skill_selected("my-skill", "dev", ["dev"]) is True

    def test_name_match(self):
        assert is_skill_selected("web-search", "community", ["web-search"]) is True

    def test_core_always_included_when_filtering_active(self):
        """Core tier is implicit when any filtering is set."""
        assert is_skill_selected("browser", "core", ["dev"]) is True

    def test_community_excluded_when_not_listed(self):
        assert is_skill_selected("some-skill", "community", ["core"]) is False

    def test_dev_excluded_when_not_listed(self):
        assert is_skill_selected("code-improver", "dev", ["core"]) is False

    def test_union_of_tier_and_name(self):
        """Tiers and names are unioned."""
        ws = ["core", "web-search"]
        assert is_skill_selected("web-search", "community", ws) is True
        assert is_skill_selected("python-heredoc", "core", ws) is True
        assert is_skill_selected("code-improver", "dev", ws) is False

    def test_empty_list_still_includes_core(self):
        """Even an empty skills list includes core (filtering is active)."""
        assert is_skill_selected("browser", "core", []) is True
        assert is_skill_selected("other", "community", []) is False


class TestSyncSkillsFiltering:
    """Test _sync_skills with workspace_skills filtering."""

    def _create_skill(self, base: Path, name: str, tier: str) -> None:
        skill_dir = base / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ntier: {tier}\n---\n# {name}\n")

    def test_none_copies_core_only(self, tmp_path: Path):
        """workspace_skills=None copies only core-tier skills (safe default)."""
        skills_src = tmp_path / "src" / "pynchy" / "agent" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=None)

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser"}

    def test_core_only_filters_correctly(self, tmp_path: Path):
        """workspace_skills=["core"] copies only core-tier skills."""
        skills_src = tmp_path / "src" / "pynchy" / "agent" / "skills"
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
        skills_src = tmp_path / "src" / "pynchy" / "agent" / "skills"
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
        skills_src = tmp_path / "src" / "pynchy" / "agent" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")
        self._create_skill(skills_src, "extra", "community")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["core", "extra"])

        copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
        assert copied == {"browser", "extra"}

    def test_star_copies_everything(self, tmp_path: Path):
        """workspace_skills=["*"] includes all skills."""
        skills_src = tmp_path / "src" / "pynchy" / "agent" / "skills"
        self._create_skill(skills_src, "browser", "core")
        self._create_skill(skills_src, "improver", "dev")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            _sync_skills(session_dir, workspace_skills=["*"])

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

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

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

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

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
        """Hook settings from agent/scripts/settings.json are merged."""
        scripts_dir = tmp_path / "src" / "pynchy" / "agent" / "scripts"
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
        scripts_dir = tmp_path / "src" / "pynchy" / "agent" / "scripts"
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
        assert shell_quote("hello") == "'hello'"

    def test_string_with_spaces(self):
        assert shell_quote("hello world") == "'hello world'"

    def test_string_with_single_quotes(self):
        # O'Brien → 'O'\''Brien'
        result = shell_quote("O'Brien")
        assert result == "'" + "O" + "'\\''" + "Brien" + "'"

    def test_empty_string(self):
        assert shell_quote("") == "''"

    def test_string_with_special_chars(self):
        """Special shell chars should be safely quoted."""
        result = shell_quote("$HOME && rm -rf /")
        assert result.startswith("'")
        assert result.endswith("'")
        assert "$HOME" in result


# ---------------------------------------------------------------------------
# Container output parsing edge cases
# ---------------------------------------------------------------------------


def _parsed_output_with_all_fields() -> ContainerOutput:
    return parse_container_output(
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


def test_parse_container_output_reads_tool_use_fields() -> None:
    out = _parsed_output_with_all_fields()

    assert out.status == "success"
    assert out.type == "tool_use"
    assert out.thinking == "Let me think..."
    assert out.tool_name == "Read"
    assert out.tool_input == {"file_path": "/test.py"}


def test_parse_container_output_reads_system_fields() -> None:
    out = _parsed_output_with_all_fields()

    assert out.system_subtype == "compact"
    assert out.system_data == {"key": "val"}
    assert out.text == "some text"


def test_parse_container_output_reads_tool_result_fields() -> None:
    out = _parsed_output_with_all_fields()

    assert out.tool_result_id == "tr-1"
    assert out.tool_result_content == "file contents"
    assert out.tool_result_is_error is False


def test_parse_container_output_reads_result_metadata() -> None:
    out = _parsed_output_with_all_fields()

    assert out.result == "done"
    assert out.new_session_id == "s1"
    assert out.result_metadata == {"duration_ms": 1234}


# ---------------------------------------------------------------------------
# input_to_dict edge case tests
# ---------------------------------------------------------------------------


class TestInputToDictEdgeCases:
    """Tests for input_to_dict with various combinations of optional fields."""

    def test_minimal_input(self):
        """Only required fields, all optionals at defaults."""
        inp = ContainerInput(
            messages=[{"content": "hi"}],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=False,
        )
        d = input_to_dict(inp)
        assert d["messages"] == [{"content": "hi"}]
        assert d["group_folder"] == "test"
        assert d["chat_jid"] == "test@g.us"
        assert d["is_admin"] is False
        # None-valued optional fields should not be present
        assert "session_id" not in d
        assert "system_notices" not in d
        assert "repo_access" not in d
        # Non-None defaults (False, strings) are included
        assert d["is_scheduled_task"] is False
        assert "agent_core_module" in d

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
        d = input_to_dict(inp)
        assert d["session_id"] == "s-1"
        assert d["is_scheduled_task"] is True
        assert d["system_notices"] == ["notice 1"]
        assert d["repo_access"] == "owner/pynchy"

    def test_is_scheduled_task_false_included(self):
        """is_scheduled_task=False is included (non-None values are always included)."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            is_scheduled_task=False,
        )
        d = input_to_dict(inp)
        assert d["is_scheduled_task"] is False

    def test_repo_access_none_omitted(self):
        """repo_access=None should NOT be included."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
            repo_access=None,
        )
        d = input_to_dict(inp)
        assert "repo_access" not in d

    def test_agent_core_fields_always_present(self):
        """agent_core_module and agent_core_class should always be in output."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="j@g.us",
            is_admin=False,
        )
        d = input_to_dict(inp)
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
        d = input_to_dict(inp)
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
        d = input_to_dict(inp)
        assert "agent_core_config" not in d


# ---------------------------------------------------------------------------
# ContainerSession — public API (Task 6)
# ---------------------------------------------------------------------------


class TestContainerSessionSignalQueryDone:
    """Tests for ContainerSession.signal_query_done() public method."""

    async def test_signal_query_done_sets_event(self):
        """signal_query_done() should set the _query_done event."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("test-group", "pynchy-test-group")
        assert not session._query_done.is_set()

        session.signal_query_done()

        assert session._query_done.is_set()

    async def test_signal_query_done_clears_output_handler(self):
        """signal_query_done() should clear the _on_output callback."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("test-group", "pynchy-test-group")
        session._on_output = AsyncMock()  # simulate active handler

        session.signal_query_done()

        assert session._on_output is None

    async def test_signal_query_done_resets_idle_timer(self):
        """signal_query_done() should restart the idle timer.

        With idle_timeout=0, _reset_idle_timer cancels any existing handle
        but does not schedule a new one.
        """
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("test-group", "pynchy-test-group")
        session._idle_timeout = 0

        # Create a real timer handle to verify cancellation
        loop = asyncio.get_running_loop()
        session._idle_handle = loop.call_later(9999, lambda: None)
        assert session._idle_handle is not None

        session.signal_query_done()

        # _reset_idle_timer cancels the old handle and, since timeout=0,
        # does not schedule a new one
        assert session._idle_handle is None

    async def test_idle_callback_called_on_expiry(self):
        """When the idle timer expires, the on_idle_expire callback should
        be called before the session is destroyed."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("idle-cb-test", "pynchy-idle-cb-test")
        callback = AsyncMock()
        session.set_idle_callback(callback)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        session.proc = mock_proc

        with patch(
            "pynchy.host.container_manager.session.destroy_session", new_callable=AsyncMock
        ) as mock_destroy:
            session._on_idle_expired()
            # Let the background task run
            await asyncio.sleep(0.05)

        callback.assert_awaited_once()
        mock_destroy.assert_awaited_once_with("idle-cb-test")

    async def test_signal_query_done_after_set_output_handler(self):
        """Full cycle: set handler, signal done, verify state reset."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("test-group", "pynchy-test-group")
        handler = AsyncMock()

        # Simulate a query in progress
        session.set_output_handler(handler)
        assert not session._query_done.is_set()
        assert session._on_output is handler

        # Signal query done
        session.signal_query_done()
        assert session._query_done.is_set()
        assert session._on_output is None


class TestGetSessionOutputHandler:
    """Tests for the module-level get_session_output_handler() function."""

    def test_returns_handler_when_session_active(self):
        """Should return the session's _on_output when an active session exists."""
        from pynchy.host.container_manager.session import (
            ContainerSession,
            _sessions,  # allow: private-test-imports
            get_session_output_handler,
        )

        session = ContainerSession("handler-test", "pynchy-handler-test")
        mock_proc = MagicMock()
        mock_proc.returncode = None  # simulate a running process
        session.proc = mock_proc  # type: ignore[assignment]
        handler = AsyncMock()
        session._on_output = handler
        _sessions["handler-test"] = session

        try:
            result = get_session_output_handler("handler-test")
            assert result is handler
        finally:
            _sessions.pop("handler-test", None)

    def test_returns_none_when_no_session(self):
        """Should return None when no session exists for the group."""
        from pynchy.host.container_manager.session import get_session_output_handler

        result = get_session_output_handler("nonexistent-group")
        assert result is None

    def test_returns_none_when_no_handler_set(self):
        """Should return None when session exists but no handler is set."""
        from pynchy.host.container_manager.session import (
            ContainerSession,
            _sessions,  # allow: private-test-imports
            get_session_output_handler,
        )

        session = ContainerSession("no-handler-test", "pynchy-no-handler-test")
        mock_proc = MagicMock()
        mock_proc.returncode = None  # simulate a running process
        session.proc = mock_proc  # type: ignore[assignment]
        session._on_output = None
        _sessions["no-handler-test"] = session

        try:
            result = get_session_output_handler("no-handler-test")
            assert result is None
        finally:
            _sessions.pop("no-handler-test", None)


class TestSessionStartOnlyStderr:
    """Tests that session.start() only starts stderr reader and proc monitor (not stdout)."""

    async def test_start_creates_stderr_task(self):
        """start() should create a stderr reader task."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("start-test", "pynchy-start-test")
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]

        assert session._stderr_task is not None
        assert not session._stderr_task.done()

        # Clean up
        proc.close()

    async def test_start_creates_proc_monitor_task(self):
        """start() should create a proc monitor task."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("monitor-test", "pynchy-monitor-test")
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]

        assert session._proc_monitor_task is not None
        assert not session._proc_monitor_task.done()

        # Clean up
        proc.close()

    async def test_start_does_not_create_stdout_task(self):
        """start() should NOT create a stdout reader task (output is via IPC files now)."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("no-stdout-test", "pynchy-no-stdout-test")
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]

        # The session should not have a _stdout_task attribute at all
        assert not hasattr(session, "_stdout_task")

        # Clean up
        proc.close()

    async def test_proc_monitor_detects_death_during_query(self):
        """When the container dies mid-query, proc monitor should set _died_before_pulse."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("death-test", "pynchy-death-test")
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]

        # Simulate a query in progress
        session.set_output_handler(AsyncMock())

        # Kill the container with a non-zero exit code
        proc.close(code=1)

        # Wait for the proc monitor to detect the exit
        await asyncio.sleep(0.05)

        assert session._dead is True
        assert session._died_before_pulse is True
        assert session._query_done.is_set()

    async def test_proc_monitor_clean_exit_no_died_before_pulse(self):
        """A clean exit (code 0) during query should NOT set _died_before_pulse."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("clean-exit-test", "pynchy-clean-exit-test")
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]

        # Simulate a query in progress
        session.set_output_handler(AsyncMock())

        # Clean exit
        proc.close(code=0)

        # Wait for the proc monitor to detect the exit
        await asyncio.sleep(0.05)

        assert session._dead is True
        assert session._died_before_pulse is False
        assert session._query_done.is_set()

    async def test_runtime_container_survives_cli_process_exit(self):
        """Apple Container can keep the container alive after the CLI process exits."""
        from pynchy.host.container_manager.session import ContainerSession

        session = ContainerSession("apple-cli-test", "pynchy-apple-cli-test")
        proc = FakeProcess()
        runtime_running = True

        def fake_runtime_running(_container_name: str) -> bool:
            return runtime_running

        with (
            patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
            patch(
                "pynchy.host.container_manager.session._runtime_container_running",
                side_effect=fake_runtime_running,
            ),
            patch("pynchy.host.container_manager.session._RUNTIME_POLL_INTERVAL_SECONDS", 0.01),
        ):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            proc.close(code=1)
            await asyncio.sleep(0.05)

            assert session.is_alive is True
            assert session._dead is False
            assert session._died_before_pulse is False
            assert not session._query_done.is_set()

            session.signal_query_done()
            runtime_running = False
            await asyncio.sleep(0.05)

        assert session._dead is True
        assert session._died_before_pulse is False
        assert session._query_done.is_set()

    async def test_runtime_monitor_waits_without_async_sleep(self):
        """Runtime polling should use an async wait primitive, not sleep-polling."""
        from pynchy.host.container_manager import session as session_mod

        session = session_mod.ContainerSession("apple-runtime-wait-test", "pynchy-runtime-wait")
        proc = FakeProcess()
        runtime_running = True

        def fake_runtime_running(_container_name: str) -> bool:
            return runtime_running

        def fail_sleep(_delay: float) -> None:
            raise AssertionError("runtime monitor should not use asyncio.sleep for polling")

        with (
            patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
            patch(
                "pynchy.host.container_manager.session._runtime_container_running",
                side_effect=fake_runtime_running,
            ),
            patch("pynchy.host.container_manager.session._RUNTIME_POLL_INTERVAL_SECONDS", 0.01),
            patch.object(session_mod.asyncio, "sleep", side_effect=fail_sleep),
        ):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            proc.close(code=1)
            await asyncio.wait_for(session._runtime_monitor_task, timeout=0.5)

            assert session.is_alive is True
            assert session._dead is False
            assert not session._query_done.is_set()

            session.signal_query_done()
            runtime_running = False
            await asyncio.wait_for(session._proc_monitor_task, timeout=0.5)

        assert session._dead is True
        assert session._died_before_pulse is False
        assert session._query_done.is_set()

    async def test_runtime_container_stop_unblocks_query_when_cli_process_hangs(self):
        """Apple Container can stop the container while the CLI process keeps hanging."""
        from pynchy.host.container_manager.session import ContainerSession, SessionDiedError

        session = ContainerSession("apple-runtime-stop-test", "pynchy-apple-runtime-stop-test")
        proc = FakeProcess()
        runtime_running = True

        def fake_runtime_running(_container_name: str) -> bool:
            return runtime_running

        with (
            patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
            patch(
                "pynchy.host.container_manager.session._runtime_container_running",
                side_effect=fake_runtime_running,
            ),
            patch("pynchy.host.container_manager.session._RUNTIME_POLL_INTERVAL_SECONDS", 0.01),
            patch("pynchy.host.container_manager.session._RUNTIME_CLI_KILL_WAIT_SECONDS", 0.01),
            patch(
                "pynchy.host.container_manager.session._reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            await asyncio.sleep(0.02)
            runtime_running = False

            with pytest.raises(SessionDiedError):
                await session.wait_for_query_done(query_timeout_seconds=0.5)

        assert proc._killed is True
        reap_orphan.assert_awaited_once_with("pynchy-apple-runtime-stop-test")
        assert session._dead is True
        assert session._died_before_pulse is True
        assert session._query_done.is_set()

    async def test_runtime_container_never_starts_unblocks_query_when_cli_process_hangs(self):
        """Apple Container can leave ``container run`` alive after startup failure."""
        from pynchy.host.container_manager.session import ContainerSession, SessionDiedError

        session = ContainerSession("apple-runtime-never-start-test", "pynchy-never-start")
        proc = FakeProcess()

        def fake_runtime_running(_container_name: str) -> bool:
            return False

        with (
            patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
            patch(
                "pynchy.host.container_manager.session._runtime_container_running",
                side_effect=fake_runtime_running,
            ),
            patch("pynchy.host.container_manager.session._RUNTIME_POLL_INTERVAL_SECONDS", 0.01),
            patch("pynchy.host.container_manager.session._RUNTIME_START_GRACE_SECONDS", 0.02),
            patch("pynchy.host.container_manager.session._RUNTIME_CLI_KILL_WAIT_SECONDS", 0.01),
            patch(
                "pynchy.host.container_manager.session._reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            with pytest.raises(SessionDiedError):
                await session.wait_for_query_done(query_timeout_seconds=0.5)

        assert proc._killed is True
        reap_orphan.assert_awaited_once_with("pynchy-never-start")
        assert session._dead is True
        assert session._died_before_pulse is True
        assert session._query_done.is_set()
