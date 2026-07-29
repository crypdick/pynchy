"""Skill activation runtime boundary contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from conftest import configure_skill_activation_for, make_settings

from pynchy.config.api import LearningConfig
from pynchy.host.learning.api import prepare_agent_homes

if TYPE_CHECKING:
    from pathlib import Path


def test_prepare_agent_homes_requires_configured_runtime(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.learning.skill_activation._runtime", None)

    with pytest.raises(RuntimeError, match="skill activation runtime has not been configured"):
        prepare_agent_homes("group")


def test_empty_learning_review_suffix_uses_no_profile_override(tmp_path: Path) -> None:
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        learning=LearningConfig(enabled=False),
    )
    configure_skill_activation_for(settings)

    homes = prepare_agent_homes("learning-review-")

    assert homes.learning_paths is None
