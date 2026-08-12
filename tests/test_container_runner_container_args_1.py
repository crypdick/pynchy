"""Tests for the container runner."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from conftest import (
    make_settings,
)
from pydantic import ValidationError

from pynchy.agent_protocol.api import (
    ContainerInput,
    VolumeMount,
)
from pynchy.config.api import (
    ContainerConfig,
)
from pynchy.host.container_manager.credentials import build_agent_env_vars, has_api_credentials
from pynchy.host.container_manager.mounts import (
    build_container_args,
)
from pynchy.process_environment import filtered_process_environment
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _MockGateway,
    _patch_settings,
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


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


class TestContainerArgs:
    def test_agent_environment_ignores_git_config_start_errors(self, tmp_path: Path):
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=None),
            patch(f"{_CR_CREDS}.subprocess.run", side_effect=OSError("git unavailable")),
        ):
            environment = build_agent_env_vars(is_admin=False, group_folder="dev")

        assert "GIT_AUTHOR_NAME" not in environment
        assert "GIT_AUTHOR_EMAIL" not in environment

    def test_agent_environment_ignores_failed_git_config(self, tmp_path: Path):
        failed = MagicMock(returncode=1, stdout="", stderr="not configured")
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=None),
            patch(f"{_CR_CREDS}.subprocess.run", return_value=failed),
        ):
            environment = build_agent_env_vars(is_admin=False, group_folder="dev")

        assert "GIT_AUTHOR_NAME" not in environment
        assert "GIT_AUTHOR_EMAIL" not in environment

    def test_agent_environment_uses_discovered_git_identity(self, tmp_path: Path):
        identity = [
            MagicMock(returncode=0, stdout="Test User\n"),
            MagicMock(returncode=0, stdout="test@example.com\n"),
        ]
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=None),
            patch(f"{_CR_CREDS}.subprocess.run", side_effect=identity),
        ):
            environment = build_agent_env_vars(is_admin=False, group_folder="dev")

        assert environment["GIT_AUTHOR_NAME"] == "Test User"
        assert environment["GIT_COMMITTER_NAME"] == "Test User"
        assert environment["GIT_AUTHOR_EMAIL"] == "test@example.com"
        assert environment["GIT_COMMITTER_EMAIL"] == "test@example.com"

    def test_agent_environment_includes_anthropic_gateway_and_extra_values(
        self,
        tmp_path: Path,
    ):
        gateway = _MockGateway(providers={"anthropic", "openai"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gateway),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            environment = build_agent_env_vars(
                is_admin=False,
                group_folder="dev",
                extra_env_vars={"EXTRA": "selected"},
            )

        assert environment["ANTHROPIC_BASE_URL"] == gateway.base_url
        assert environment["ANTHROPIC_AUTH_TOKEN"] == gateway.key
        assert environment["EXTRA"] == "selected"

    def test_agent_environment_omits_unavailable_openai_gateway(self, tmp_path: Path):
        gateway = _MockGateway(providers={"anthropic"})
        with (
            _patch_settings(tmp_path),
            patch(f"{_GATEWAY}.get_gateway", return_value=gateway),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            environment = build_agent_env_vars(is_admin=False, group_folder="dev")

        assert environment["ANTHROPIC_BASE_URL"] == gateway.base_url
        assert "OPENAI_BASE_URL" not in environment

    def test_has_api_credentials_reflects_gateway_provider(self):
        gateway = _MockGateway(providers={"anthropic"})
        with patch(f"{_GATEWAY}.get_gateway", return_value=gateway):
            assert has_api_credentials() is True

    def test_agent_environment_keeps_gateway_but_never_discovers_github(
        self,
        tmp_path: Path,
    ):
        gateway = _MockGateway(providers={"openai"})
        with (
            _patch_settings(tmp_path, secret_overrides={"gh_token": "legacy-token"}),
            patch(f"{_GATEWAY}.get_gateway", return_value=gateway),
            patch(f"{_CR_CREDS}._read_git_identity", return_value=(None, None)),
        ):
            environment = build_agent_env_vars(is_admin=True, group_folder="admin")

        assert environment["OPENAI_BASE_URL"] == gateway.base_url
        assert environment["OPENAI_API_KEY"] == gateway.key
        assert "GH_TOKEN" not in environment
        assert "GITHUB_TOKEN" not in environment

    def test_selected_environment_uses_names_in_argv_and_values_in_child_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
        selected = {"LINEAR_API_KEY": "selected-value"}  # pragma: allowlist secret

        args = build_container_args(
            [],
            "test-container",
            memory_mb=2048,
            image="pynchy-agent:latest",
            env_names=tuple(selected),
        )
        child_env = filtered_process_environment(selected)

        assert args[args.index("-e") : args.index("-e") + 2] == ["-e", "LINEAR_API_KEY"]
        assert selected["LINEAR_API_KEY"] not in args
        assert child_env["LINEAR_API_KEY"] == selected["LINEAR_API_KEY"]
        assert "UNRELATED_HOST_SECRET" not in child_env

    def test_readonly_uses_mount_flag(self):
        mounts = [VolumeMount("/host/path", "/container/path", readonly=True)]
        args = build_container_args(
            mounts, "test-container", memory_mb=2048, image="pynchy-agent:latest"
        )
        assert "--mount" in args
        assert any("readonly" in a for a in args)
        assert "-v" not in args[args.index("--mount") :]  # no -v after --mount for this mount

    def test_readwrite_uses_v_flag(self):
        mounts = [VolumeMount("/host/path", "/container/path", readonly=False)]
        args = build_container_args(
            mounts, "test-container", memory_mb=2048, image="pynchy-agent:latest"
        )
        assert "-v" in args
        assert "/host/path:/container/path" in args

    def test_apple_readonly_file_mount_uses_volume_flag(self, tmp_path: Path):
        host_file = tmp_path / "custom-ca.pem"
        host_file.write_text("ca")
        ca_container_path = str(PurePosixPath("/", "tmp", "custom-ca.pem"))
        mounts = [VolumeMount(str(host_file), ca_container_path, readonly=True)]
        runtime = MagicMock(name="runtime")
        runtime.name = "apple"

        with patch(
            "pynchy.host.container_manager.mounts._configured_mount_operations"
        ) as configured:
            configured.return_value.runtime_name.return_value = runtime.name
            args = build_container_args(
                mounts, "test-container", memory_mb=2048, image="pynchy-agent:latest"
            )

        assert "-v" in args
        assert f"{host_file}:{ca_container_path}:ro" in args
        assert "--mount" not in args

    def test_apple_runtime_uses_two_gib_agent_memory_ceiling(self):
        runtime = MagicMock(name="runtime")
        runtime.name = "apple"

        with patch(
            "pynchy.host.container_manager.mounts._configured_mount_operations"
        ) as configured:
            configured.return_value.runtime_name.return_value = runtime.name
            args = build_container_args(
                [], "test-container", memory_mb=2048, image="pynchy-agent:latest"
            )

        memory_index = args.index("--memory")
        assert args[memory_index + 1] == "2048m"

    def test_explicit_memory_limit_applies_to_other_runtimes(self):
        runtime = MagicMock(name="runtime")
        runtime.name = "docker"
        settings = make_settings(container=ContainerConfig(memory_mb=1536))

        with patch(
            "pynchy.host.container_manager.mounts._configured_mount_operations"
        ) as configured:
            configured.return_value.runtime_name.return_value = runtime.name
            args = build_container_args(
                [],
                "test-container",
                memory_mb=settings.container.memory_mb,
                image=settings.container.image,
            )

        memory_index = args.index("--memory")
        assert args[memory_index + 1] == "1536m"

    def test_agent_memory_limit_cannot_exceed_two_gib(self):
        with pytest.raises(ValidationError, match="less than or equal to 2048"):
            ContainerConfig(memory_mb=2049)

    def test_includes_name_and_image(self):
        args = build_container_args([], "my-container", memory_mb=2048, image="pynchy-agent:latest")
        assert args[:3] == ["run", "--name", "my-container"]
        assert "--label" in args
        assert "com.pynchy.role=agent" in args
        # Last arg is the image
        assert args[-1].endswith("-agent:latest")
