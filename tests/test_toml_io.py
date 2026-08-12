"""Public TOML configuration I/O behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from pynchy.config.api import mutate_config_toml, parse_settings_toml

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_settings_toml_uses_schema_defaults_for_empty_input() -> None:
    settings = parse_settings_toml("")

    assert settings.server.port == 8484


def test_mutate_config_toml_writes_a_validated_document(tmp_path: Path) -> None:
    path = tmp_path / "pynchy.toml"

    settings = mutate_config_toml(path, lambda doc: doc.add("server", {"port": 9000}))

    assert settings.server.port == 9000
    assert "port = 9000" in path.read_text(encoding="utf-8")


def test_mutate_config_toml_does_not_write_an_invalid_candidate(tmp_path: Path) -> None:
    path = tmp_path / "pynchy.toml"
    path.write_text("[server]\nport = 8484\n", encoding="utf-8")

    with pytest.raises(ValidationError, match=r"server\.port"):
        mutate_config_toml(path, lambda doc: doc["server"].update({"port": 0}))

    assert path.read_text(encoding="utf-8") == "[server]\nport = 8484\n"


def test_mutate_config_toml_preserves_existing_file_when_publish_fails(tmp_path: Path) -> None:
    path = tmp_path / "pynchy.toml"
    original = "[server]\nport = 8484\n"
    path.write_text(original, encoding="utf-8")

    with (
        patch("pynchy.atomic_json.os.replace", side_effect=OSError("publish failed")),
        pytest.raises(OSError, match="publish failed"),
    ):
        mutate_config_toml(path, lambda doc: doc["server"].update({"port": 9000}))

    assert path.read_text(encoding="utf-8") == original
