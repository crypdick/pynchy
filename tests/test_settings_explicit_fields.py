"""Tests for explicit-fields validation in Settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config import Settings
from pynchy.config.models import AgentConfig, CronJobConfig


class TestExplicitFieldValidation:
    def test_rejects_partial_direct_submodel(self) -> None:
        with pytest.raises(ValidationError, match=r"agent: missing \['name', 'trigger_aliases'\]"):
            Settings(agent=AgentConfig(core="openai"))

    def test_rejects_partial_dict_entry_submodel(self) -> None:
        with pytest.raises(
            ValidationError,
            match=r"cron_jobs\.nightly: missing \['enabled', 'timeout_seconds'\]",
        ):
            Settings(cron_jobs={"nightly": CronJobConfig(schedule="0 0 * * *", command="echo hi")})
