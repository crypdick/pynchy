"""Tests for Obsidian learning path resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config import Settings
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


def test_resolver_returns_none_when_learning_is_disabled(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=LearningConfig(enabled=False))

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        assert resolve_learning_paths("shopping") is None


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


def test_vault_root_expands_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(Path("~/vault")),
        workspaces={"shopping-group": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.vault_root == (home / "vault").resolve()
    assert paths.profile_root == (home / "vault/systems/pynchy/profiles/shopping").resolve()


def test_custom_mount_path_is_used_for_mounted_paths(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault, mount_path="/mnt/obsidian"),
        workspaces={"shopping-group": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.vault_mount_path == "/mnt/obsidian"
    assert paths.mounted_profile_root == "/mnt/obsidian/systems/pynchy/profiles/shopping"
    assert paths.mounted_memory_root == "/mnt/obsidian/systems/pynchy/profiles/shopping/memory"
    assert paths.mounted_skills_root == "/mnt/obsidian/systems/pynchy/profiles/shopping/skills"


def test_resolver_does_not_create_directories(tmp_path):
    vault = tmp_path / "missing-vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert not vault.exists()
    assert not paths.profile_root.exists()
    assert not paths.memory_root.exists()
    assert not paths.skills_root.exists()


def test_symlink_escape_through_profile_root_is_rejected(tmp_path):
    vault = tmp_path / "vault"
    escaped = tmp_path / "escaped"
    vault.mkdir()
    escaped.mkdir()
    (vault / "profiles").symlink_to(escaped, target_is_directory=True)
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault, default_profile_root="profiles/{profile}"),
        workspaces={"shopping-group": WorkspaceConfig(profile="shopping")},
    )

    with (
        patch("pynchy.host.learning.paths.get_settings", return_value=settings),
        pytest.raises(LearningConfigError, match="inside"),
    ):
        resolve_learning_paths("shopping-group")


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


def test_profile_override_wins_over_workspace_profile_and_is_slugged(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        paths = resolve_learning_paths("shopping-group", profile_override="Deep Work!!")

    assert paths is not None
    assert paths.profile == "Deep Work!!"
    assert paths.profile_slug == "deep-work"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/deep-work"


def test_default_profile_root_must_be_relative_and_stay_inside_vault():
    with pytest.raises(ValidationError, match="relative"):
        ObsidianLearningConfig(default_profile_root="/systems/pynchy/profiles/{profile}")

    for template in ("../profiles/{profile}", "systems/../profiles/{profile}"):
        with pytest.raises(ValidationError, match=r"\.\."):
            ObsidianLearningConfig(default_profile_root=template)


def test_settings_validation_allows_learning_enabled_without_vault_root(tmp_path):
    settings = Settings.model_validate(
        {
            "learning": {
                "enabled": True,
                "review_after_turn": True,
                "max_attempts": 3,
                "packet_max_chars": 12_000,
                "skill_max_bytes": 200_000,
                "obsidian": {
                    "mount_path": "/workspace/vault",
                    "default_profile_root": "systems/pynchy/profiles/{profile}",
                    "memory_dir_name": "memory",
                    "skills_dir_name": "skills",
                },
            },
        }
    )

    with (
        patch("pynchy.host.learning.paths.get_settings", return_value=settings),
        pytest.raises(LearningConfigError, match=r"obsidian\.vault_root"),
    ):
        resolve_learning_paths("shopping")


def test_learning_dir_names_must_be_single_path_components():
    invalid_values = ["", "memory/cache", ".", ".."]

    for value in invalid_values:
        with pytest.raises(ValidationError, match="single path component"):
            ObsidianLearningConfig(memory_dir_name=value)
        with pytest.raises(ValidationError, match="single path component"):
            ObsidianLearningConfig(skills_dir_name=value)


def test_learning_operational_knobs_must_be_positive():
    invalid_cases = [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"packet_max_chars": 0},
        {"packet_max_chars": -1},
        {"skill_max_bytes": 0},
        {"skill_max_bytes": -1},
    ]

    for kwargs in invalid_cases:
        with pytest.raises(ValidationError, match="positive"):
            LearningConfig(**kwargs)


def test_mount_path_must_be_absolute_posix_container_path():
    with pytest.raises(ValidationError, match="absolute"):
        ObsidianLearningConfig(mount_path="workspace/vault")

    for mount_path in ("/", "/workspace/../vault", "/workspace\\vault"):
        with pytest.raises(ValidationError, match="mount_path"):
            ObsidianLearningConfig(mount_path=mount_path)


def test_mount_path_normalizes_repeated_and_trailing_slashes():
    cfg = ObsidianLearningConfig(mount_path="//workspace//vault//")

    assert cfg.mount_path == "/workspace/vault"
