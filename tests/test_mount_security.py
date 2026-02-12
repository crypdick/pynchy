"""Tests for mount security — allowed/blocked paths, readonly enforcement.

Deferred from Phase 4. Covers allowlist loading, path validation, blocked patterns,
and readonly enforcement for main vs. non-main groups.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.mount_security import (
    DEFAULT_BLOCKED_PATTERNS,
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


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level allowlist cache between tests."""
    _reset_cache()
    yield
    _reset_cache()


# ---------------------------------------------------------------------------
# Path expansion
# ---------------------------------------------------------------------------


class TestExpandPath:
    def test_expands_tilde(self):
        with patch.dict(os.environ, {"HOME": "/Users/testuser"}):
            assert _expand_path("~/projects") == "/Users/testuser/projects"

    def test_expands_bare_tilde(self):
        with patch.dict(os.environ, {"HOME": "/Users/testuser"}):
            assert _expand_path("~") == "/Users/testuser"

    def test_absolute_path_unchanged(self):
        assert _expand_path("/absolute/path") == "/absolute/path"

    def test_relative_path_resolved(self):
        result = _expand_path("relative/path")
        assert os.path.isabs(result)


# ---------------------------------------------------------------------------
# Blocked patterns
# ---------------------------------------------------------------------------


class TestBlockedPatterns:
    def test_matches_exact_component(self):
        assert _matches_blocked_pattern("/home/user/.ssh/id_rsa", [".ssh"]) == ".ssh"

    def test_matches_substring_in_path(self):
        assert (
            _matches_blocked_pattern("/home/user/credentials-store/data", ["credentials"])
            == "credentials"
        )

    def test_no_match_returns_none(self):
        assert _matches_blocked_pattern("/home/user/projects/myapp", [".ssh", ".env"]) is None

    def test_matches_env_file(self):
        assert _matches_blocked_pattern("/home/user/project/.env", [".env"]) == ".env"

    def test_default_blocked_patterns_include_sensitive_dirs(self):
        sensitive = [".ssh", ".gnupg", ".aws", ".kube", ".docker", "credentials", ".env"]
        for s in sensitive:
            assert s in DEFAULT_BLOCKED_PATTERNS


# ---------------------------------------------------------------------------
# Allowed root matching
# ---------------------------------------------------------------------------


class TestFindAllowedRoot:
    def test_path_under_allowed_root_matches(self, tmp_path: Path):
        root = AllowedRoot(path=str(tmp_path), allow_read_write=True)
        child = tmp_path / "subdir"
        child.mkdir()
        result = _find_allowed_root(str(child), [root])
        assert result is root

    def test_exact_root_path_matches(self, tmp_path: Path):
        root = AllowedRoot(path=str(tmp_path))
        result = _find_allowed_root(str(tmp_path), [root])
        assert result is root

    def test_path_outside_root_returns_none(self, tmp_path: Path):
        root = AllowedRoot(path=str(tmp_path / "allowed"))
        (tmp_path / "allowed").mkdir()
        result = _find_allowed_root(str(tmp_path / "other"), [root])
        assert result is None

    def test_nonexistent_root_skipped(self, tmp_path: Path):
        root = AllowedRoot(path=str(tmp_path / "nonexistent"))
        result = _find_allowed_root(str(tmp_path / "nonexistent" / "child"), [root])
        assert result is None


# ---------------------------------------------------------------------------
# Container path validation
# ---------------------------------------------------------------------------


class TestContainerPathValidation:
    def test_valid_relative_path(self):
        assert _is_valid_container_path("mydata") is True

    def test_nested_relative_path(self):
        assert _is_valid_container_path("some/nested/path") is True

    def test_rejects_dotdot(self):
        assert _is_valid_container_path("../escape") is False

    def test_rejects_absolute_path(self):
        assert _is_valid_container_path("/absolute/path") is False

    def test_rejects_empty(self):
        assert _is_valid_container_path("") is False

    def test_rejects_whitespace_only(self):
        assert _is_valid_container_path("   ") is False


# ---------------------------------------------------------------------------
# Allowlist loading
# ---------------------------------------------------------------------------


class TestLoadAllowlist:
    def test_loads_valid_allowlist(self, tmp_path: Path):
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({
            "allowedRoots": [
                {"path": "~/projects", "allowReadWrite": True, "description": "Dev"},
            ],
            "blockedPatterns": ["custom-secret"],
            "nonMainReadOnly": True,
        }))
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = load_mount_allowlist()

        assert result is not None
        assert len(result.allowed_roots) == 1
        assert result.allowed_roots[0].path == "~/projects"
        assert result.allowed_roots[0].allow_read_write is True
        assert result.non_main_read_only is True
        # Custom patterns are merged with defaults
        assert "custom-secret" in result.blocked_patterns
        assert ".ssh" in result.blocked_patterns  # default still present

    def test_returns_none_when_file_missing(self, tmp_path: Path):
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", tmp_path / "nope.json"):
            result = load_mount_allowlist()
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path: Path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", bad_file):
            result = load_mount_allowlist()
        assert result is None

    def test_returns_none_on_missing_fields(self, tmp_path: Path):
        incomplete = tmp_path / "incomplete.json"
        incomplete.write_text(json.dumps({"allowedRoots": []}))
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", incomplete):
            result = load_mount_allowlist()
        assert result is None

    def test_caches_result(self, tmp_path: Path):
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({
            "allowedRoots": [],
            "blockedPatterns": [],
            "nonMainReadOnly": True,
        }))
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            first = load_mount_allowlist()
            second = load_mount_allowlist()
        assert first is second


# ---------------------------------------------------------------------------
# Full mount validation
# ---------------------------------------------------------------------------


class TestValidateMount:
    def _write_allowlist(
        self, tmp_path: Path, *, roots: list[dict], non_main_read_only: bool = True
    ):
        """Write an allowlist file and return its path."""
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({
            "allowedRoots": roots,
            "blockedPatterns": [],
            "nonMainReadOnly": non_main_read_only,
        }))
        return allowlist_file

    def test_allows_path_under_root(self, tmp_path: Path):
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        target = allowed_dir / "myfile"
        target.mkdir()

        allowlist_file = self._write_allowlist(
            tmp_path, roots=[{"path": str(allowed_dir), "allowReadWrite": True}]
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="myfile"),
                is_main=True,
            )
        assert result.allowed is True

    def test_rejects_path_outside_root(self, tmp_path: Path):
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        allowlist_file = self._write_allowlist(
            tmp_path, roots=[{"path": str(allowed_dir), "allowReadWrite": True}]
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(outside), container_path="outside"),
                is_main=True,
            )
        assert result.allowed is False
        assert "not under any allowed root" in result.reason

    def test_rejects_blocked_pattern(self, tmp_path: Path):
        allowed_dir = tmp_path / "allowed"
        ssh_dir = allowed_dir / ".ssh"
        ssh_dir.mkdir(parents=True)

        allowlist_file = self._write_allowlist(
            tmp_path, roots=[{"path": str(allowed_dir), "allowReadWrite": True}]
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(ssh_dir), container_path="ssh-keys"),
                is_main=True,
            )
        assert result.allowed is False
        assert ".ssh" in result.reason

    def test_rejects_nonexistent_path(self, tmp_path: Path):
        allowlist_file = self._write_allowlist(
            tmp_path, roots=[{"path": str(tmp_path), "allowReadWrite": True}]
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(tmp_path / "ghost"), container_path="ghost"),
                is_main=True,
            )
        assert result.allowed is False
        assert "does not exist" in result.reason

    def test_rejects_no_allowlist(self, tmp_path: Path):
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", tmp_path / "nope.json"):
            result = validate_mount(
                AdditionalMount(host_path="/some/path", container_path="x"),
                is_main=True,
            )
        assert result.allowed is False
        assert "No mount allowlist" in result.reason

    def test_rejects_invalid_container_path(self, tmp_path: Path):
        target = tmp_path / "ok"
        target.mkdir()
        allowlist_file = self._write_allowlist(
            tmp_path, roots=[{"path": str(tmp_path), "allowReadWrite": True}]
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="../escape"),
                is_main=True,
            )
        assert result.allowed is False
        assert "Invalid container path" in result.reason

    def test_defaults_container_path_to_basename(self, tmp_path: Path):
        target = tmp_path / "mydata"
        target.mkdir()
        allowlist_file = self._write_allowlist(
            tmp_path, roots=[{"path": str(tmp_path), "allowReadWrite": True}]
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target)),  # no container_path
                is_main=True,
            )
        assert result.allowed is True
        assert result.resolved_container_path == "mydata"


# ---------------------------------------------------------------------------
# Readonly enforcement
# ---------------------------------------------------------------------------


class TestReadonlyEnforcement:
    def _setup_allowlist(self, tmp_path: Path, *, allow_read_write: bool, non_main_read_only: bool):
        """Create dirs and allowlist for readonly tests."""
        target = tmp_path / "data"
        target.mkdir()
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({
            "allowedRoots": [
                {"path": str(tmp_path), "allowReadWrite": allow_read_write},
            ],
            "blockedPatterns": [],
            "nonMainReadOnly": non_main_read_only,
        }))
        return target, allowlist_file

    def test_main_group_can_get_readwrite_when_root_allows(self, tmp_path: Path):
        target, allowlist_file = self._setup_allowlist(
            tmp_path, allow_read_write=True, non_main_read_only=True
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data", readonly=False),
                is_main=True,
            )
        assert result.allowed is True
        assert result.effective_readonly is False

    def test_nonmain_forced_readonly_when_non_main_read_only_is_true(self, tmp_path: Path):
        target, allowlist_file = self._setup_allowlist(
            tmp_path, allow_read_write=True, non_main_read_only=True
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data", readonly=False),
                is_main=False,
            )
        assert result.allowed is True
        assert result.effective_readonly is True  # Forced readonly for non-main

    def test_nonmain_can_get_readwrite_when_non_main_read_only_is_false(self, tmp_path: Path):
        target, allowlist_file = self._setup_allowlist(
            tmp_path, allow_read_write=True, non_main_read_only=False
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data", readonly=False),
                is_main=False,
            )
        assert result.allowed is True
        assert result.effective_readonly is False

    def test_root_without_readwrite_forces_readonly(self, tmp_path: Path):
        target, allowlist_file = self._setup_allowlist(
            tmp_path, allow_read_write=False, non_main_read_only=False
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data", readonly=False),
                is_main=True,
            )
        assert result.allowed is True
        assert result.effective_readonly is True  # Root doesn't allow rw

    def test_default_readonly_when_not_explicitly_readwrite(self, tmp_path: Path):
        target, allowlist_file = self._setup_allowlist(
            tmp_path, allow_read_write=True, non_main_read_only=False
        )
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            # readonly=True (default)
            result = validate_mount(
                AdditionalMount(host_path=str(target), container_path="data", readonly=True),
                is_main=True,
            )
        assert result.allowed is True
        assert result.effective_readonly is True


# ---------------------------------------------------------------------------
# Batch validation
# ---------------------------------------------------------------------------


class TestValidateAdditionalMounts:
    def test_filters_out_rejected_mounts(self, tmp_path: Path):
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        good = allowed_dir / "good"
        good.mkdir()

        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({
            "allowedRoots": [
                {"path": str(allowed_dir), "allowReadWrite": True},
            ],
            "blockedPatterns": [],
            "nonMainReadOnly": True,
        }))

        mounts = [
            AdditionalMount(host_path=str(good), container_path="good"),
            AdditionalMount(host_path="/nonexistent/path", container_path="bad"),
        ]
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_additional_mounts(mounts, "TestGroup", is_main=True)

        assert len(result) == 1
        assert result[0]["containerPath"] == "/workspace/extra/good"

    def test_empty_mounts_returns_empty(self, tmp_path: Path):
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({
            "allowedRoots": [],
            "blockedPatterns": [],
            "nonMainReadOnly": True,
        }))
        with patch("pynchy.mount_security.MOUNT_ALLOWLIST_PATH", allowlist_file):
            result = validate_additional_mounts([], "TestGroup", is_main=True)
        assert result == []


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


class TestGenerateTemplate:
    def test_template_is_valid_json(self):
        template = generate_allowlist_template()
        data = json.loads(template)
        assert "allowedRoots" in data
        assert "blockedPatterns" in data
        assert "nonMainReadOnly" in data
