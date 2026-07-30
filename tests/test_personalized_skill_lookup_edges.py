"""Canonical personalized-skill lookup boundary contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.host.learning.api import find_personalized_skill_dir
from pynchy.host.learning.skills import configure_personalized_skills_root

if TYPE_CHECKING:
    from pathlib import Path


def test_personalized_skill_lookup_requires_configured_root(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.learning.skills._skills_root", None)

    with pytest.raises(RuntimeError, match="personalized skills root has not been configured"):
        find_personalized_skill_dir("skill")


def test_personalized_skill_lookup_rejects_non_file_skill_manifest(tmp_path: Path) -> None:
    skill_dir = tmp_path / "data/personalization/skills/example"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").mkdir()
    configure_personalized_skills_root(tmp_path)

    assert find_personalized_skill_dir("example") is None
