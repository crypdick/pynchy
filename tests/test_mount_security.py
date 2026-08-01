"""Tests for mount security using TOML-only allowlist."""

from __future__ import annotations

import os
import tomllib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.container_manager.security import mount_security
from pynchy.host.container_manager.security.mount_security import (
    generate_allowlist_template,
)
from pynchy.host.container_manager.security.mount_security import (
    load_mount_allowlist as _load_mount_allowlist,
)
from pynchy.host.container_manager.security.mount_security import (
    validate_additional_mounts as _validate_additional_mounts,
)
from pynchy.host.container_manager.security.mount_security import (
    validate_mount as _validate_mount,
)
from pynchy.workspace.api import AdditionalMount

if TYPE_CHECKING:
    from pathlib import Path


def _test_settings(allowlist_path: Path):
    return make_settings(mount_allowlist_path=allowlist_path)


def _mount_policy_inputs() -> tuple[Path, tuple[str, ...]]:
    settings = mount_security.get_settings()
    return settings.mount_allowlist_path, tuple(settings.security.blocked_patterns)


def load_mount_allowlist():
    allowlist_path, blocked_patterns = _mount_policy_inputs()
    return _load_mount_allowlist(allowlist_path, blocked_patterns)


def validate_mount(mount: AdditionalMount, *, is_admin: bool):
    allowlist_path, blocked_patterns = _mount_policy_inputs()
    return _validate_mount(
        mount,
        is_admin=is_admin,
        allowlist_path=allowlist_path,
        default_blocked_patterns=blocked_patterns,
    )


def validate_additional_mounts(mounts: list[AdditionalMount], group_name: str, *, is_admin: bool):
    allowlist_path, blocked_patterns = _mount_policy_inputs()
    return _validate_additional_mounts(
        mounts,
        group_name,
        is_admin=is_admin,
        allowlist_path=allowlist_path,
        default_blocked_patterns=blocked_patterns,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset mount policy between isolated configuration scenarios."""
    mount_security.reset_mount_allowlist_cache()
    yield
    mount_security.reset_mount_allowlist_cache()


def _write_allowlist(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TestHelpers:
    """Path/pattern helpers, exercised through the public validate_mount()."""

    def test_home_expansion_in_allowed_root(self, tmp_path: Path):
        """A ~ in an allowed root path is expanded to the home directory."""
        projects = tmp_path / "projects"
        projects.mkdir()
        target = projects / "repo"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "~/projects"
allow_read_write = true
""".strip(),
        )
        with (
            patch.dict(os.environ, {"HOME": str(tmp_path)}),
            patch(
                "pynchy.host.container_manager.security.mount_security.get_settings",
                return_value=_test_settings(allowlist),
                create=True,
            ),
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="repo"), is_admin=True
            )
        assert result.allowed is True

    def test_blocked_pattern_rejects_mount(self, tmp_path: Path):
        """A path segment matching a blocked pattern is rejected."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = allowed / "secret-data"
        secret.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = ["secret-data"]

[[allowed_roots]]
path = "{allowed}"
allow_read_write = true
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(secret), container_path="x"), is_admin=True
            )
        assert result.allowed is False
        assert "blocked pattern" in result.reason

    def test_mount_under_allowed_root_resolves(self, tmp_path: Path):
        """A path under an allowed root resolves and reports its real path."""
        root = tmp_path / "root"
        root.mkdir()
        child = root / "a"
        child.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{root}"
allow_read_write = true
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(child), container_path="a"), is_admin=True
            )
        assert result.allowed is True
        assert result.real_host_path == os.path.realpath(str(child))

    def test_container_path_validation(self, tmp_path: Path):
        """Container paths must be relative and free of parent-dir escapes."""
        root = tmp_path / "root"
        root.mkdir()
        target = root / "data"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{root}"
allow_read_write = true
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            ok = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data"), is_admin=True
            )
            bad = validate_mount(
                AdditionalMount(host_path=str(target), container_path="../escape"), is_admin=True
            )
            absolute = validate_mount(
                AdditionalMount(host_path=str(target), container_path="/escape"), is_admin=True
            )
        assert ok.allowed is True
        assert bad.allowed is False
        assert absolute.allowed is False
        assert "container path" in bad.reason.lower()


class TestLoadAllowlist:
    def test_loads_toml(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_admin_read_only = true
blocked_patterns = ["custom-secret"]

[[allowed_roots]]
path = "~/projects"
allow_read_write = true
description = "Dev"
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            data = load_mount_allowlist()
        assert data is not None
        assert data.non_admin_read_only is True
        assert data.allowed_roots[0].allow_read_write is True
        assert "custom-secret" in data.blocked_patterns

    def test_missing_file_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "missing.toml"
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None

    def test_invalid_toml_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(allowlist, "not = [valid")
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None
            assert load_mount_allowlist() is None

    def test_invalid_allowed_root_entry_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = 123
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None

    def test_invalid_allowed_root_table_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_admin_read_only = true
blocked_patterns = []
allowed_roots = ["not-a-table"]
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None

    def test_invalid_optional_root_boolean_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "~/projects"
allow_read_write = "not-a-bool"
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None

    def test_invalid_blocked_pattern_entry_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            """
non_admin_read_only = true
blocked_patterns = ["ok", 123]

[[allowed_roots]]
path = "~/projects"
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None

    @pytest.mark.parametrize(
        "content",
        [
            """
non_admin_read_only = true
blocked_patterns = "not-a-list"
allowed_roots = []
""",
            """
non_admin_read_only = "not-a-bool"
blocked_patterns = []
allowed_roots = []
""",
        ],
    )
    def test_invalid_required_top_level_types_return_none(self, tmp_path: Path, content: str):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(allowlist, content.strip())
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            assert load_mount_allowlist() is None


class TestValidateMount:
    def test_missing_allowlist_rejects_mount(self, tmp_path: Path):
        result = mount_security.validate_mount(
            AdditionalMount(host_path=str(tmp_path), container_path="data"),
            is_admin=True,
            allowlist_path=tmp_path / "missing.toml",
            default_blocked_patterns=(),
        )

        assert result.allowed is False
        assert result.reason == f"No mount allowlist configured at {tmp_path / 'missing.toml'}"

    def test_blocked_pattern_can_match_a_path_substring(self, tmp_path: Path):
        target = tmp_path / "allowed" / "repo"
        target.mkdir(parents=True)
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = ["allowed/repo"]

[[allowed_roots]]
path = "{tmp_path / "allowed"}"
allow_read_write = true
""".strip(),
        )

        result = mount_security.validate_mount(
            AdditionalMount(host_path=str(target), container_path="repo"),
            is_admin=True,
            allowlist_path=allowlist,
            default_blocked_patterns=(),
        )

        assert result.allowed is False
        assert 'blocked pattern "allowed/repo"' in result.reason

    def test_missing_allowed_root_rejects_existing_mount(self, tmp_path: Path):
        target = tmp_path / "target"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{tmp_path / "missing-root"}"
allow_read_write = true
""".strip(),
        )

        result = mount_security.validate_mount(
            AdditionalMount(host_path=str(target), container_path="target"),
            is_admin=True,
            allowlist_path=allowlist,
            default_blocked_patterns=(),
        )

        assert result.allowed is False
        assert "not under any allowed root" in result.reason

    def test_read_write_mount_is_forced_readonly_by_root_policy(self, tmp_path: Path):
        target = tmp_path / "target"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = false
blocked_patterns = []

[[allowed_roots]]
path = "{tmp_path}"
allow_read_write = false
""".strip(),
        )

        result = mount_security.validate_mount(
            AdditionalMount(host_path=str(target), container_path="target", readonly=False),
            is_admin=True,
            allowlist_path=allowlist,
            default_blocked_patterns=(),
        )

        assert result.allowed is True
        assert result.effective_readonly is True

    def test_allows_mount_under_root(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "repo"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{allowed}"
allow_read_write = true
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="repo"), is_admin=True
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
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{allowed}"
allow_read_write = true
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(outside), container_path="outside"),
                is_admin=True,
            )
        assert result.allowed is False

    def test_non_admin_forced_readonly(self, tmp_path: Path):
        data = tmp_path / "data"
        data.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{tmp_path}"
allow_read_write = true
""".strip(),
        )
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(data), container_path="data", readonly=False),
                is_admin=False,
            )
        assert result.allowed is True
        assert result.effective_readonly is True

    def test_admin_read_write_mount_remains_writable_when_root_allows_it(self, tmp_path: Path):
        data = tmp_path / "data"
        data.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = false
blocked_patterns = []

[[allowed_roots]]
path = "{tmp_path}"
allow_read_write = true
""".strip(),
        )

        result = mount_security.validate_mount(
            AdditionalMount(host_path=str(data), container_path="data", readonly=False),
            is_admin=True,
            allowlist_path=allowlist,
            default_blocked_patterns=(),
        )

        assert result.allowed is True
        assert result.effective_readonly is False

    def test_unresolvable_allowed_root_is_skipped(self, tmp_path: Path):
        target = tmp_path / "target"
        target.mkdir()
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(
            allowlist,
            f"""
non_admin_read_only = true
blocked_patterns = []

[[allowed_roots]]
path = "{tmp_path}"
allow_read_write = true
""".strip(),
        )
        original_resolve = mount_security.Path.resolve
        root_calls = 0

        def resolve(path, *args, **kwargs):
            nonlocal root_calls
            if path == tmp_path:
                root_calls += 1
            if root_calls == 2:
                raise OSError("root disappeared")
            return original_resolve(path, *args, **kwargs)

        with patch.object(mount_security.Path, "resolve", resolve):
            result = mount_security.validate_mount(
                AdditionalMount(host_path=str(target), container_path="target"),
                is_admin=True,
                allowlist_path=allowlist,
                default_blocked_patterns=(),
            )

        assert result.allowed is False
        assert "not under any allowed root" in result.reason


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
non_admin_read_only = true
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
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
            create=True,
        ):
            result = validate_additional_mounts(mounts, "TestGroup", is_admin=True)
        assert len(result) == 1
        assert result[0]["containerPath"] == "/home/agent/mnt/good"


class TestTemplate:
    def test_template_is_valid_toml(self):
        data = tomllib.loads(generate_allowlist_template())
        assert "allowed_roots" in data
        assert "blocked_patterns" in data
        assert "non_admin_read_only" in data
