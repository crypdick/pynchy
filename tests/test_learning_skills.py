"""Tests for profile-scoped learned skill discovery."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig, ObsidianLearningConfig, WorkspaceConfig


def _enabled_learning(vault_root: Path, *, skill_max_bytes: int = 200_000) -> LearningConfig:
    return LearningConfig(
        enabled=True,
        skill_max_bytes=skill_max_bytes,
        obsidian=ObsidianLearningConfig(vault_root=str(vault_root)),
    )


def _settings(*, tmp_path: Path, learning: LearningConfig, workspaces: dict | None = None):
    return make_settings(
        learning=learning,
        workspaces=workspaces or {},
        sandbox_profiles={},
        project_root=tmp_path,
        data_dir=tmp_path / "data",
    )


def _iter_learned_skill_dirs(group_folder: str) -> list[Path]:
    module = importlib.import_module("pynchy.host.learning.skills")
    return module.iter_learned_skill_dirs(group_folder)


def test_iter_returns_empty_when_learning_disabled(tmp_path: Path):
    settings = _settings(
        tmp_path=tmp_path,
        learning=LearningConfig(enabled=False),
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        assert _iter_learned_skill_dirs("shopping") == []


def test_iter_returns_empty_when_skills_root_is_missing(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        assert _iter_learned_skill_dirs("shopping") == []


def test_iter_returns_only_skill_dirs_with_skill_md(tmp_path: Path):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/shopping/skills"
    valid = skills_root / "valid-skill"
    invalid = skills_root / "missing-metadata"
    valid.mkdir(parents=True)
    invalid.mkdir()
    (valid / "SKILL.md").write_text("---\nname: valid-skill\ntier: learned\n---\n")
    (invalid / "README.md").write_text("not a skill")
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping": WorkspaceConfig(profile="shopping")},
    )

    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        assert _iter_learned_skill_dirs("shopping") == [valid.resolve()]


def test_iter_skips_symlink_that_escapes_skills_root(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skills_root.mkdir(parents=True)
    escaped = tmp_path / "escaped-skill"
    escaped.mkdir()
    (escaped / "SKILL.md").write_text("---\nname: escaped\ntier: learned\n---\n")
    (skills_root / "escaped").symlink_to(escaped, target_is_directory=True)
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    caplog.set_level(logging.WARNING)
    with patch("pynchy.host.learning.paths.get_settings", return_value=settings):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "outside skills root" in caplog.text


def test_iter_skips_skill_over_byte_budget(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    oversized = skills_root / "too-large"
    oversized.mkdir(parents=True)
    (oversized / "SKILL.md").write_text("---\nname: too-large\ntier: learned\n---\n")
    (oversized / "payload.txt").write_text("abcdef")
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault, skill_max_bytes=5),
    )

    caplog.set_level(logging.WARNING)
    with (
        patch("pynchy.host.learning.paths.get_settings", return_value=settings),
        patch("pynchy.host.learning.skills.get_settings", return_value=settings),
    ):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "exceeds byte budget" in caplog.text
