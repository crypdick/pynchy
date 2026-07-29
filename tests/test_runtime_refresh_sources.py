"""Runtime configuration source change detection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.config.api import configuration_source_digest

if TYPE_CHECKING:
    from pathlib import Path


def test_configuration_source_digest_is_stable_then_changes_with_personalization(
    tmp_path: Path,
) -> None:
    defaults = tmp_path / "data" / "defaults"
    personalization = tmp_path / "data" / "personalization"
    defaults.mkdir(parents=True)
    personalization.mkdir(parents=True)
    config = personalization / "pynchy.toml"
    config.write_text("[agent]\nname = 'first'\n", encoding="utf-8")

    baseline = configuration_source_digest(tmp_path)

    assert configuration_source_digest(tmp_path) == baseline
    config.write_text("[agent]\nname = 'second'\n", encoding="utf-8")
    assert configuration_source_digest(tmp_path) != baseline
