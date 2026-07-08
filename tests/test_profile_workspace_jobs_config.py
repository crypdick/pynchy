"""Tests for the profile/workspace/jobs config grammar."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config import Settings
from pynchy.config.jobs import JobConfig
from pynchy.config.models import (
    ConnectionsConfig,
    ProfileConfig,
    SlackConnectionConfig,
    WorkspaceConfig,
)


def _settings_with_slack(**overrides) -> Settings:
    defaults = {
        "connection": ConnectionsConfig(
            slack={
                "synapse": SlackConnectionConfig(
                    bot_token_env="SLACK_BOT_TOKEN",
                    app_token_env="SLACK_APP_TOKEN",
                    chat={"admin": {}, "pynchy": {}},
                )
            }
        ),
        "profiles": {"admin": ProfileConfig(is_admin=True)},
        "workspaces": {
            "admin": WorkspaceConfig(
                profile="admin",
                chat="connection.slack.synapse.chat.admin",
            )
        },
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_settings_use_profiles_and_workspaces_as_public_config_names() -> None:
    settings = _settings_with_slack(
        universal=ProfileConfig(tags=["base"]),
        profiles={
            "admin": ProfileConfig(
                tags=["pynchy", "browser"],
                is_admin=True,
                contains_secrets=True,
                repo_access="crypdick/pynchy",
                model="chatgpt/gpt-5.3-codex-spark",
            )
        },
    )

    assert settings.universal.tags == ["base"]
    assert settings.profiles["admin"].contains_secrets is True
    assert settings.profiles["admin"].repo_access == "crypdick/pynchy"
    assert settings.workspaces["admin"].profile == "admin"


def test_legacy_sandbox_sections_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Legacy config sections"):
        Settings(
            connection=_settings_with_slack().connection,
            profiles={"admin": ProfileConfig(is_admin=True)},
            sandbox={
                "admin": WorkspaceConfig(
                    profile="admin",
                    chat="connection.slack.synapse.chat.admin",
                )
            },
        )


def test_host_is_reserved_and_cannot_be_a_workspace_name() -> None:
    with pytest.raises(ValidationError, match="'host' is reserved"):
        _settings_with_slack(
            workspaces={
                "host": WorkspaceConfig(
                    profile="admin",
                    chat="connection.slack.synapse.chat.admin",
                )
            }
        )


def test_workspace_profile_reference_must_exist() -> None:
    with pytest.raises(
        ValidationError, match=r"workspaces\.admin\.profile references unknown profile"
    ):
        _settings_with_slack(
            profiles={},
            workspaces={
                "admin": WorkspaceConfig(
                    profile="missing",
                    chat="connection.slack.synapse.chat.admin",
                )
            },
        )


def test_agent_job_targets_configured_workspace() -> None:
    settings = _settings_with_slack(
        jobs={
            "daily-triage": JobConfig(
                enabled=True,
                schedule="0 8 * * *",
                workspace="admin",
                prompt_file="prompts/daily-triage.md",
            )
        }
    )

    job = settings.jobs["daily-triage"]
    assert job.workspace == "admin"
    assert job.schedule == "0 8 * * *"
    assert job.prompt_file == "prompts/daily-triage.md"


def test_one_time_agent_job_uses_at_instead_of_schedule() -> None:
    job = JobConfig(
        enabled=True,
        at="2026-07-08T18:30:00-07:00",
        workspace="admin",
        prompt="Cancel the subscription.",
    )

    assert job.at == "2026-07-08T18:30:00-07:00"
    assert job.schedule is None


def test_job_requires_exactly_one_schedule_shape() -> None:
    with pytest.raises(ValidationError, match="exactly one of schedule or at"):
        JobConfig(
            enabled=True,
            schedule="0 8 * * *",
            at="2026-07-08T18:30:00-07:00",
            workspace="admin",
            prompt="Nope.",
        )


def test_host_job_is_selected_by_workspace_magic_word() -> None:
    settings = _settings_with_slack(
        jobs={
            "backup-runtime-dbs": JobConfig(
                enabled=True,
                schedule="0 3 * * *",
                workspace="host",
                command="scripts/backup_runtime_dbs.sh",
                cwd=".",
                timeout_seconds=600,
                quiet_on_success=True,
            )
        }
    )

    job = settings.jobs["backup-runtime-dbs"]
    assert job.is_host is True
    assert job.command == "scripts/backup_runtime_dbs.sh"
    assert job.quiet_on_success is True
    assert settings.cron_jobs["backup-runtime-dbs"].command == "scripts/backup_runtime_dbs.sh"
    assert settings.cron_jobs["backup-runtime-dbs"].quiet_on_success is True


def test_agent_job_requires_prompt_or_prompt_file() -> None:
    with pytest.raises(ValidationError, match="agent jobs require prompt or prompt_file"):
        JobConfig(enabled=True, schedule="0 8 * * *", workspace="admin")


def test_host_job_rejects_agent_prompt_fields() -> None:
    with pytest.raises(ValidationError, match="host jobs cannot set prompt"):
        JobConfig(
            enabled=True,
            schedule="0 3 * * *",
            workspace="host",
            command="scripts/backup_runtime_dbs.sh",
            prompt="Do it.",
        )


def test_host_job_rejects_one_time_at_until_dispatch_exists() -> None:
    with pytest.raises(ValidationError, match="host jobs require schedule"):
        JobConfig(
            enabled=True,
            at="2026-07-08T18:30:00-07:00",
            workspace="host",
            command="scripts/backup_runtime_dbs.sh",
        )
