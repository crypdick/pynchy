"""Tests for directive resolution logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pynchy.config import DirectiveConfig
from pynchy.directives import _scope_matches, resolve_directives

_PATCH_TARGET = "pynchy.directives.get_settings"


class TestScopeMatches:
    def test_all_matches_everything(self):
        assert _scope_matches("all", "any-workspace", None)
        assert _scope_matches("all", "admin-1", "crypdick/pynchy")

    def test_workspace_name_matches(self):
        assert _scope_matches("admin-1", "admin-1", None)
        assert not _scope_matches("admin-1", "admin-2", None)

    def test_repo_slug_matches(self):
        assert _scope_matches("crypdick/pynchy", "ci", "crypdick/pynchy")
        assert not _scope_matches("crypdick/pynchy", "ci", "other/repo")
        assert not _scope_matches("crypdick/pynchy", "ci", None)

    def test_workspace_name_with_no_repo(self):
        assert _scope_matches("my-group", "my-group", None)
        assert not _scope_matches("my-group", "other-group", None)


class TestResolveDirectives:
    @pytest.fixture()
    def tmp_project(self, tmp_path: Path) -> Path:
        """Create a temp project directory with directive files."""
        d = tmp_path / "directives"
        d.mkdir()
        (d / "base.md").write_text("# Base\nShared instructions.")
        (d / "admin.md").write_text("# Admin\nAdmin-only content.")
        (d / "repo.md").write_text("# Repo\nRepo-specific content.")
        (d / "example.md.EXAMPLE").write_text("# Example\nIgnored.")
        return tmp_path

    def _settings(
        self, root: Path, directives: dict[str, DirectiveConfig]
    ) -> MagicMock:
        s = MagicMock()
        s.directives = directives
        s.project_root = root
        return s

    def test_scope_all_matches_everything(self, tmp_project: Path):
        cfg = {"base": DirectiveConfig(file="directives/base.md", scope="all")}
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("any-workspace", None)
        assert result == "# Base\nShared instructions."

    def test_scope_workspace_name_matches(self, tmp_project: Path):
        cfg = {"admin": DirectiveConfig(file="directives/admin.md", scope="admin-1")}
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("admin-1", None)
        assert result == "# Admin\nAdmin-only content."

    def test_scope_workspace_name_no_match(self, tmp_project: Path):
        cfg = {"admin": DirectiveConfig(file="directives/admin.md", scope="admin-1")}
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("admin-2", None)
        assert result is None

    def test_scope_repo_slug_matches(self, tmp_project: Path):
        cfg = {
            "repo": DirectiveConfig(
                file="directives/repo.md", scope="crypdick/pynchy"
            ),
        }
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("code-improver", "crypdick/pynchy")
        assert result == "# Repo\nRepo-specific content."

    def test_scope_list_union(self, tmp_project: Path):
        cfg = {
            "admin": DirectiveConfig(
                file="directives/admin.md",
                scope=["admin-1", "crypdick/pynchy"],
            ),
        }
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            assert resolve_directives("admin-1", None) is not None
            assert resolve_directives("ci", "crypdick/pynchy") is not None
            assert resolve_directives("other-group", None) is None

    def test_no_scope_warns_and_skips(self, tmp_project: Path):
        cfg = {"broken": DirectiveConfig(file="directives/base.md", scope=None)}
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("any-workspace", None)
        assert result is None

    def test_directives_sorted_by_key(self, tmp_project: Path):
        cfg = {
            "z-last": DirectiveConfig(file="directives/admin.md", scope="all"),
            "a-first": DirectiveConfig(file="directives/base.md", scope="all"),
        }
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("any-workspace", None)
        assert result is not None
        assert result.startswith("# Base")
        assert "---" in result
        assert result.endswith("Admin-only content.")

    def test_no_matching_returns_none(self, tmp_project: Path):
        cfg = {"admin": DirectiveConfig(file="directives/admin.md", scope="admin-1")}
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("unrelated-group", None)
        assert result is None

    def test_missing_file_warns_and_skips(self, tmp_project: Path):
        cfg = {
            "missing": DirectiveConfig(
                file="directives/nonexistent.md", scope="all"
            ),
        }
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("any-workspace", None)
        assert result is None

    def test_empty_directives_config(self, tmp_project: Path):
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, {})):
            result = resolve_directives("any-workspace", None)
        assert result is None

    def test_example_suffix_ignored(self, tmp_project: Path):
        cfg = {
            "example": DirectiveConfig(
                file="directives/example.md.EXAMPLE", scope="all"
            ),
        }
        with patch(_PATCH_TARGET, return_value=self._settings(tmp_project, cfg)):
            result = resolve_directives("any-workspace", None)
        assert result is None
