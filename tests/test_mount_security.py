"""Tests for mount security using TOML-only allowlist."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.container_manager.security import mount_security
from pynchy.host.container_manager.security.mount_security import (
    _optional_bool_value,  # allow: private-test-imports - direct type contracts
    _parse_allowed_root,  # allow: private-test-imports - direct type contracts
    _parse_allowlist_table,  # allow: private-test-imports - direct type contracts
    _required_bool,  # allow: private-test-imports - direct type contracts
    _required_list,  # allow: private-test-imports - direct type contracts
    _string_value,  # allow: private-test-imports - direct type contracts
    generate_allowlist_template,
    load_mount_allowlist,
    validate_additional_mounts,
    validate_mount,
)
from pynchy.types import AdditionalMount


def _test_settings(allowlist_path: Path):
    return make_settings(mount_allowlist_path=allowlist_path)


@pytest.fixture(autouse=True)
def _clear_cache():
    # Reset the module-level allowlist cache for test isolation. The reset is a
    # test-only affordance on the module; reaching it by attribute avoids
    # importing a private symbol.
    mount_security._reset_cache()
    yield
    mount_security._reset_cache()


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
        ):
            ok = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data"), is_admin=True
            )
            bad = validate_mount(
                AdditionalMount(host_path=str(target), container_path="../escape"), is_admin=True
            )
        assert ok.allowed is True
        assert bad.allowed is False
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
        ):
            assert load_mount_allowlist() is None

    def test_invalid_toml_returns_none(self, tmp_path: Path):
        allowlist = tmp_path / "mount-allowlist.toml"
        _write_allowlist(allowlist, "not = [valid")
        with patch(
            "pynchy.host.container_manager.security.mount_security.get_settings",
            return_value=_test_settings(allowlist),
        ):
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
        ):
            assert load_mount_allowlist() is None


class TestTypeValidation:
    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            (_required_list, "must be an array"),
            (_required_bool, "must be a boolean"),
            (_string_value, "must be a string"),
        ],
    )
    def test_basic_type_checks_raise_typeerror(self, call, expected):
        table = {"value": "not-the-right-type"}
        if call is _required_list or call is _required_bool:
            with pytest.raises(TypeError, match=expected):
                call(table, "value")
        else:
            with pytest.raises(TypeError, match=expected):
                call(123, field_name="value")

    def test_optional_bool_type_check_raises_typeerror(self):
        with pytest.raises(TypeError, match="must be a boolean"):
            _optional_bool_value("nope", field_name="value", default=True)

    def test_parse_allowed_root_requires_mapping(self):
        with pytest.raises(TypeError, match="must be a table"):
            _parse_allowed_root([], index=0)

    def test_parse_allowlist_table_requires_mapping(self):
        with pytest.raises(TypeError, match="must decode to a TOML table"):
            _parse_allowlist_table([])


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
        ):
            result = validate_mount(
                AdditionalMount(host_path=str(data), container_path="data", readonly=False),
                is_admin=False,
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
        ):
            result = validate_additional_mounts(mounts, "TestGroup", is_admin=True)
        assert len(result) == 1
        assert result[0]["containerPath"] == "/workspace/extra/good"


class TestTemplate:
    def test_template_is_valid_toml(self):
        data = tomllib.loads(generate_allowlist_template())
        assert "allowed_roots" in data
        assert "blocked_patterns" in data
        assert "non_admin_read_only" in data
