"""Tests for explicit-fields validation in Settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config import Settings
from pynchy.config.jobs import JobConfig
from pynchy.config.models import AgentConfig


class TestExplicitFieldValidation:
    def test_rejects_partial_direct_submodel(self) -> None:
        with pytest.raises(ValidationError, match=r"agent: missing \['name', 'trigger_aliases'\]"):
            Settings(agent=AgentConfig(core="openai"))

    def test_rejects_partial_dict_entry_submodel(self) -> None:
        with pytest.raises(
            ValidationError,
            match=r"jobs\.nightly: missing \['enabled'\]",
        ):
            Settings(
                jobs={
                    "nightly": JobConfig(
                        schedule="0 0 * * *",
                        workspace="host",
                        command="echo hi",
                    )
                }
            )
