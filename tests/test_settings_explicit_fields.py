"""Tests for explicit field handling in Settings."""

from __future__ import annotations

from pynchy.config.jobs import JobConfig
from pynchy.config.models import AgentConfig, SecurityConfig
from pynchy.config.settings import validate_settings_mapping


class TestExplicitFieldValidation:
    def test_accepts_defaulted_direct_submodel(self) -> None:
        settings = validate_settings_mapping(
            {"agent": AgentConfig(default_core="codex")},
        )

        assert settings.agent.default_core == "codex"
        assert settings.agent.model is None

    def test_accepts_defaulted_dict_entry_submodel(self) -> None:
        settings = validate_settings_mapping(
            {
                "jobs": {
                    "nightly": JobConfig(
                        schedule="0 0 * * *",
                        workspace="host",
                        command="echo hi",
                    )
                }
            }
        )

        assert settings.jobs["nightly"].enabled is True
        assert settings.cron_jobs["nightly"].enabled is True

    def test_accepts_defaulted_agent_mapping(self) -> None:
        settings = validate_settings_mapping(
            {"agent": {"default_core": "openai"}},
        )

        assert settings.agent == AgentConfig(default_core="openai")

    def test_accepts_dedicated_cop_model(self) -> None:
        settings = validate_settings_mapping(
            {"security": {"cop_model": "gpt-5.3-codex-spark"}},
        )

        assert settings.security == SecurityConfig(cop_model="gpt-5.3-codex-spark")
