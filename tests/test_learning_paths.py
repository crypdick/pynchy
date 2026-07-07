"""Tests for Obsidian learning path resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config.models import LearningConfig, ObsidianLearningConfig, WorkspaceConfig
from pynchy.host.learning.paths import (
    LearningConfigError,
    profile_name_for_group,
    resolve_learning_paths,
)


def _settings(*, tmp_path: Path, learning: LearningConfig, workspaces: dict | None = None):
    return make_settings(
        learning=learning,
        workspaces=workspaces or {},
        sandbox_profiles={},
        project_root=tmp_path,
        data_dir=tmp_path / "data",
    )


def _enabled_learning(vault_root: Path, **obsidian_overrides) -> LearningConfig:
    return LearningConfig(
        enabled=True,
        obsidian=ObsidianLearningConfig(
            vault_root=str(vault_root),
            **obsidian_overrides,
        ),
    )


def test_learning_config_defaults_to_disabled_without_vault():
    cfg = LearningConfig()

    assert cfg.enabled is False
    assert cfg.obsidian.vault_root is None


def test_enabled_learning_without_vault_fails_in_resolver(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=LearningConfig(enabled=True))

    with (
        patch("pynchy.host.learning.paths.get_settings", return_value=settings),
        pytest.raises(LearningConfigError, match=r"obsidian\.vault_root"),
    ):
        resolve_learning_paths("shopping")


def test_workspace_profile_resolves_profile_root(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.profile == "shopping"
    assert paths.profile_slug == "shopping"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/shopping"
    assert paths.memory_root == paths.profile_root / "memory"
    assert paths.skills_root == paths.profile_root / "skills"
    assert paths.mounted_profile_root == "/workspace/vault/systems/pynchy/profiles/shopping"
    assert paths.mounted_memory_root == "/workspace/vault/systems/pynchy/profiles/shopping/memory"
    assert paths.mounted_skills_root == "/workspace/vault/systems/pynchy/profiles/shopping/skills"


def test_workspace_without_profile_resolves_default_profile(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"unprofiled": WorkspaceConfig()},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        assert profile_name_for_group("unprofiled") == "default"
        paths = resolve_learning_paths("unprofiled")

    assert paths is not None
    assert paths.profile == "default"
    assert paths.profile_slug == "default"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/default"


def test_profile_slug_is_path_safe_but_original_profile_is_preserved(tmp_path):
    vault = tmp_path / "vault"
    profile = "Shopping List!! / 2026"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profile=profile)},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.profile == profile
    assert paths.profile_slug == "shopping-list-2026"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/shopping-list-2026"


def test_default_profile_root_must_be_relative_and_stay_inside_vault():
    with pytest.raises(ValidationError, match="relative"):
        ObsidianLearningConfig(default_profile_root="/systems/pynchy/profiles/{profile}")

    for template in ("../profiles/{profile}", "systems/../profiles/{profile}"):
        with pytest.raises(ValidationError, match=r"\.\."):
            ObsidianLearningConfig(default_profile_root=template)


def test_mount_path_must_be_absolute_container_path():
    with pytest.raises(ValidationError, match="absolute"):
        ObsidianLearningConfig(mount_path="workspace/vault")
