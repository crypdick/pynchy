"""Tests for profile-scoped learned skill discovery."""

from __future__ import annotations

import contextlib
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


@contextlib.contextmanager
def _patch_learning_settings(settings):
    with (
        patch("pynchy.host.learning.paths.get_settings", return_value=settings),
        patch("pynchy.host.learning.skills.get_settings", return_value=settings),
    ):
        yield


def test_iter_returns_empty_when_learning_disabled(tmp_path: Path):
    settings = _settings(
        tmp_path=tmp_path,
        learning=LearningConfig(enabled=False),
    )

    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("shopping") == []


def test_iter_returns_empty_when_skills_root_is_missing(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = _settings(
        tmp_path=tmp_path,
        learning=_enabled_learning(vault),
        workspaces={"shopping": WorkspaceConfig(profile="shopping")},
    )

    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("shopping") == []


def test_iter_returns_empty_when_skills_root_iterdir_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skills_root.mkdir(parents=True)
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))
    original_iterdir = Path.iterdir

    def fail_skills_root_iterdir(path: Path):
        if path == skills_root.resolve():
            raise OSError("iterdir denied")
        return original_iterdir(path)

    caplog.set_level(logging.WARNING)
    with (
        _patch_learning_settings(settings),
        patch.object(Path, "iterdir", fail_skills_root_iterdir),
    ):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skills root" in caplog.text
    assert "iterdir denied" in caplog.text


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

    with _patch_learning_settings(settings):
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
    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "outside skills root" in caplog.text


def test_iter_skips_skill_with_file_symlink_escape(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skill = skills_root / "leaky"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: leaky\ntier: learned\n---\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    (skill / "secret.txt").symlink_to(secret)
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    caplog.set_level(logging.WARNING)
    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "symlink" in caplog.text


def test_iter_skips_skill_md_symlink_escape(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skill = skills_root / "leaky"
    skill.mkdir(parents=True)
    escaped_skill_md = tmp_path / "SKILL.md"
    escaped_skill_md.write_text("---\nname: leaky\ntier: learned\n---\n")
    (skill / "SKILL.md").symlink_to(escaped_skill_md)
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    caplog.set_level(logging.WARNING)
    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "symlink" in caplog.text


def test_iter_skips_skill_with_nested_directory(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skill = skills_root / "nested"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: nested\ntier: learned\n---\n")
    nested_dir = skill / "reference"
    nested_dir.mkdir()
    (nested_dir / "notes.md").write_text("unsupported v1 nested content")
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    caplog.set_level(logging.WARNING)
    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "nested directory" in caplog.text


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
    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "exceeds byte budget" in caplog.text


def test_iter_skips_skill_when_file_stat_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skill = skills_root / "unreadable-stat"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: unreadable-stat\ntier: learned\n---\n")
    payload = skill / "payload.txt"
    payload.write_text("payload")
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))
    original_stat = Path.stat

    def fail_payload_stat(path: Path, *args, **kwargs):
        if path == payload and kwargs.get("follow_symlinks", True):
            raise OSError("stat denied")
        return original_stat(path, *args, **kwargs)

    caplog.set_level(logging.WARNING)
    with _patch_learning_settings(settings), patch.object(Path, "stat", fail_payload_stat):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "stat denied" in caplog.text


def test_iter_skips_skill_when_file_lstat_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    vault = tmp_path / "vault"
    skills_root = vault / "systems/pynchy/profiles/default/skills"
    skill = skills_root / "unreadable-lstat"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: unreadable-lstat\ntier: learned\n---\n")
    payload = skill / "payload.txt"
    payload.write_text("payload")
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))
    original_lstat = Path.lstat

    def fail_payload_lstat(path: Path):
        if path == payload:
            raise OSError("lstat denied")
        return original_lstat(path)

    caplog.set_level(logging.WARNING)
    with _patch_learning_settings(settings), patch.object(Path, "lstat", fail_payload_lstat):
        assert _iter_learned_skill_dirs("unprofiled") == []

    assert "Skipping learned skill" in caplog.text
    assert "lstat denied" in caplog.text


def test_iter_accepts_current_loader_metadata_without_description(tmp_path: Path):
    vault = tmp_path / "vault"
    skill = vault / "systems/pynchy/profiles/default/skills/no-description"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: no-description\ntier: learned\n---\n# No Description\n"
    )
    settings = _settings(tmp_path=tmp_path, learning=_enabled_learning(vault))

    with _patch_learning_settings(settings):
        assert _iter_learned_skill_dirs("unprofiled") == [skill.resolve()]
