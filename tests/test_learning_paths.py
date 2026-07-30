"""Tests for Obsidian learning path resolution."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import configure_learning_paths_for, make_settings
from pydantic import ValidationError

from pynchy.config.api import LearningConfig, ObsidianLearningConfig, Settings, WorkspaceConfig
from pynchy.host.learning.paths import (
    LearningConfigError,
    profile_name_for_group,
    resolve_automation_memory_paths,
    resolve_learning_paths,
)


def _settings(*, tmp_path: Path, learning: LearningConfig, workspaces: dict | None = None):
    return make_settings(
        learning=learning,
        workspaces=workspaces or {},
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


@contextmanager
def _configured_learning_paths(settings: Settings):
    configure_learning_paths_for(settings)
    yield


def test_learning_config_defaults_to_disabled_without_vault():
    cfg = LearningConfig()

    assert cfg.enabled is False
    assert cfg.obsidian.vault_root is None


def test_resolver_returns_none_when_learning_is_disabled(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=LearningConfig(enabled=False))

    with _configured_learning_paths(settings):
        assert resolve_learning_paths("shopping") is None


def test_automation_memory_returns_none_when_learning_is_disabled(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=LearningConfig(enabled=False))

    with _configured_learning_paths(settings):
        assert resolve_automation_memory_paths("job-weekly-security") is None


def test_automation_memory_requires_a_task_id(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(tmp_path / "vault"))

    with (
        _configured_learning_paths(settings),
        pytest.raises(LearningConfigError, match="task id"),
    ):
        resolve_automation_memory_paths("")


def test_automation_memory_requires_a_vault_root(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=LearningConfig(enabled=True))

    with (
        _configured_learning_paths(settings),
        pytest.raises(LearningConfigError, match=r"obsidian\.vault_root"),
    ):
        resolve_automation_memory_paths("job-weekly-security")


def test_automation_memory_is_task_owned_under_the_private_wiki_subtree(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    with _configured_learning_paths(settings):
        paths = resolve_automation_memory_paths("job-weekly-security")

    assert paths is not None
    assert paths.canonical == (
        vault.resolve() / "wiki/systems/pynchy/automation-memory/job-weekly-security"
    )
    assert paths.mirror == (
        tmp_path / "data/learning/automation-memory-mirrors/job-weekly-security"
    )


def test_automation_memory_encodes_unsafe_task_id_characters(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    with _configured_learning_paths(settings):
        paths = resolve_automation_memory_paths("task/with spaces")

    assert paths is not None
    assert paths.canonical.name == "task%2Fwith%20spaces"


def test_enabled_learning_without_vault_fails_in_resolver(tmp_path):
    settings = _settings(tmp_path=tmp_path, learning=LearningConfig(enabled=True))

    with (
        _configured_learning_paths(settings),
        pytest.raises(LearningConfigError, match=r"obsidian\.vault_root"),
    ):
        resolve_learning_paths("shopping")


def test_workspace_profile_resolves_profile_root(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with _configured_learning_paths(settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.profile == "shopping"
    assert paths.profile_slug == "shopping"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/shopping"
    assert paths.memory_root == paths.profile_root / "memory"
    assert paths.mounted_profile_root == "/workspace/vault/systems/pynchy/profiles/shopping"
    assert paths.mounted_memory_root == "/workspace/vault/systems/pynchy/profiles/shopping/memory"


def test_profile_root_template_errors_are_reported_as_learning_config_errors(tmp_path):
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(tmp_path / "vault", default_profile_root="{unknown}"),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with (
        _configured_learning_paths(settings),
        pytest.raises(LearningConfigError, match="valid template"),
    ):
        resolve_learning_paths("shopping-group")


def test_root_profile_mount_uses_the_mount_path_directly(tmp_path):
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(tmp_path / "vault", default_profile_root="."),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with _configured_learning_paths(settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.mounted_profile_root == "/workspace/vault"
    assert paths.mounted_memory_root == "/workspace/vault/memory"


def test_vault_root_expands_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(Path("~/vault")),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with _configured_learning_paths(settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.vault_root == (home / "vault").resolve()
    assert paths.profile_root == (home / "vault/systems/pynchy/profiles/shopping").resolve()


def test_custom_mount_path_is_used_for_mounted_paths(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault, mount_path="/mnt/obsidian"),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with _configured_learning_paths(settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert paths.vault_mount_path == "/mnt/obsidian"
    assert paths.mounted_profile_root == "/mnt/obsidian/systems/pynchy/profiles/shopping"
    assert paths.mounted_memory_root == "/mnt/obsidian/systems/pynchy/profiles/shopping/memory"


def test_resolver_does_not_create_directories(tmp_path):
    vault = tmp_path / "missing-vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with _configured_learning_paths(settings):
        paths = resolve_learning_paths("shopping-group")

    assert paths is not None
    assert not vault.exists()
    assert not paths.profile_root.exists()
    assert not paths.memory_root.exists()


def test_symlink_escape_through_profile_root_is_rejected(tmp_path):
    vault = tmp_path / "vault"
    escaped = tmp_path / "escaped"
    vault.mkdir()
    escaped.mkdir()
    (vault / "profiles").symlink_to(escaped, target_is_directory=True)
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault, default_profile_root="profiles/{profile}"),
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with (
        _configured_learning_paths(settings),
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

    with _configured_learning_paths(settings):
        assert profile_name_for_group("unprofiled") == "default"
        paths = resolve_learning_paths("unprofiled")

    assert paths is not None
    assert paths.profile == "default"
    assert paths.profile_slug == "default"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/default"


def test_workspace_with_multiple_profiles_uses_first_profile_for_learning_context(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"team": WorkspaceConfig(profiles=["base", "ops"])},
    )

    with _configured_learning_paths(settings):
        assert profile_name_for_group("team") == "base"
        paths = resolve_learning_paths("team")

    assert paths is not None
    assert paths.profile == "base"
    assert paths.profile_root == vault.resolve() / "systems/pynchy/profiles/base"


def test_dynamic_thread_uses_parent_workspace_learning_profile(tmp_path):
    vault = tmp_path / "vault"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"pynchy-dev": WorkspaceConfig(profiles=["pynchy-dev"])},
    )

    with _configured_learning_paths(settings):
        paths = resolve_learning_paths("pynchy-dev__thread_discord-channel-42")

    assert paths is not None
    assert paths.profile == "pynchy-dev"


def test_profile_slug_is_path_safe_but_original_profile_is_preserved(tmp_path):
    vault = tmp_path / "vault"
    profile = "Shopping List!! / 2026"
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping-group": WorkspaceConfig(profiles=[profile])},
    )

    with _configured_learning_paths(settings):
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
        workspaces={"shopping-group": WorkspaceConfig(profiles=["shopping"])},
    )

    with _configured_learning_paths(settings):
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
                "obsidian": {
                    "mount_path": "/workspace/vault",
                    "default_profile_root": "systems/pynchy/profiles/{profile}",
                    "memory_dir_name": "memory",
                },
            },
        }
    )

    with (
        _configured_learning_paths(settings),
        pytest.raises(LearningConfigError, match=r"obsidian\.vault_root"),
    ):
        resolve_learning_paths("shopping")


def test_learning_dir_names_must_be_single_path_components():
    invalid_values = ["", "memory/cache", ".", ".."]

    for value in invalid_values:
        with pytest.raises(ValidationError, match="single path component"):
            ObsidianLearningConfig(memory_dir_name=value)


def test_learning_operational_knobs_must_be_positive():
    invalid_cases = [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"packet_max_chars": 0},
        {"packet_max_chars": -1},
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
