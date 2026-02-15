"""Tests for mount security using TOML-only allowlist."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.mount_security import (
    _expand_path,
    _find_allowed_root,
    _is_valid_container_path,
    _matches_blocked_pattern,
    _reset_cache,
    generate_allowlist_template,
    load_mount_allowlist,
    validate_additional_mounts,
    validate_mount,
)
from pynchy.types import AdditionalMount, AllowedRoot


def _test_settings(allowlist_path: Path) -> Settings:
    s = Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )
    s.__dict__["mount_allowlist_path"] = allowlist_path
    return s


@pytest.fixture(autouse=True)
def _clear_cache():
    _reset_cache()
    yield
    _reset_cache()


def _write_allowlist(path: Path, content: str) -> None:
    path.write_text(content)


class TestHelpers:
    def test_expand_path(self):
        with patch.dict(os.environ, {"HOME": "/home/test"}):
            assert _expand_path("~/projects") == "/home/test/projects"

    def test_matches_blocked_pattern(self):
        assert _matches_blocked_pattern("/home/u/.ssh/id_rsa", [".ssh"]) == ".ssh"
        assert _matches_blocked_pattern("/home/u/src", [".ssh"]) is None

    def test_find_allowed_root(self, tmp_path: Path):
        root = AllowedRoot(path=str(tmp_path))
        child = tmp_path / "a"
        child.mkdir()
        assert _find_allowed_root(str(child), [root]) is root

    def test_container_path_validation(self):
        assert _is_valid_container_path("data") is True
        assert _is_valid_container_path("../escape") is False


class TestLoadAllowlist:
    def test_loads_toml(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_god_read_only = true
blocked_patterns = ["custom-secret"]

[[allowed_roots]]
path = "~/projects"
allow_read_write = true
description = "Dev"
""".strip(),
        )
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            data = load_mount_allowlist()
        assert data is not None
        assert data.non_god_read_only is True
        assert data.allowed_roots[0].allow_read_write is True
        assert "custom-secret" in data.blocked_patterns

    def test_missing_file_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "missing.toml"
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            assert load_mount_allowlist() is None

    def test_invalid_toml_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(allowlist, "not = [valid")
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            assert load_mount_allowlist() is None


class TestValidateMount:
    def test_allows_mount_under_root(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "repo"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_god_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{allowed}"
allow_read_write = true
""".strip(),
        )
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="repo"), is_god=True
            )
        assert result.allowed is True

    def test_rejects_outside_root(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_god_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{allowed}"
allow_read_write = true
""".strip(),
        )
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            result = validate_mount(
                AdditionalMount(host_path=str(outside), container_path="outside"),
                is_god=True,
            )
        assert result.allowed is False

    def test_non_god_forced_readonly(self, tmp_path: Path):
        data = tmp_path / "data"
        data.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_god_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{tmp_path}"
allow_read_write = true
""".strip(),
        )
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            result = validate_mount(
                AdditionalMount(host_path=str(data), container_path="data", readonly=False),
                is_god=False,
            )
        assert result.allowed is True
        assert result.effective_readonly is True


class TestBatchValidation:
    def test_filters_rejected_mounts(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        good = allowed / "good"
        good.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_god_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{allowed}"
allow_read_write = true
""".strip(),
        )
        mounts = [
            AdditionalMount(host_path=str(good), container_path="good"),
            AdditionalMount(host_path=str(tmp_path / "missing"), container_path="bad"),
        ]
        with patch("pynchy.mount_security.get_settings", return_value=_test_settings(allowlist)):
            result = validate_additional_mounts(mounts, "TestGroup", is_god=True)
        assert len(result) == 1
        assert result[0]["containerPath"] == "/workspace/extra/good"


class TestTemplate:
    def test_template_is_valid_toml(self):
        data = tomllib.loads(generate_allowlist_template())
        assert "allowed_roots" in data
        assert "blocked_patterns" in data
        assert "non_god_read_only" in data
