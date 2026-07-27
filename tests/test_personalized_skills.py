"""Tests for canonical personalization skill discovery."""

from __future__ import annotations

from unittest.mock import patch

from conftest import make_settings

from pynchy.host.learning.skills import find_personalized_skill_dir


def test_finds_exact_personalized_skill(tmp_path):
    skill = tmp_path / "data/personalization/skills/remember-routing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: remember-routing\n---\n")
    settings = make_settings(project_root=tmp_path)

    with patch("pynchy.host.learning.skills.get_settings", return_value=settings):
        assert find_personalized_skill_dir("remember-routing") == skill.resolve()


def test_rejects_missing_symlinked_and_traversal_skills(tmp_path):
    skills = tmp_path / "data/personalization/skills"
    target = tmp_path / "outside"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: outside\n---\n")
    skills.mkdir(parents=True)
    (skills / "linked").symlink_to(target, target_is_directory=True)
    settings = make_settings(project_root=tmp_path)

    with patch("pynchy.host.learning.skills.get_settings", return_value=settings):
        assert find_personalized_skill_dir("missing") is None
        assert find_personalized_skill_dir("linked") is None
        assert find_personalized_skill_dir("../outside") is None
