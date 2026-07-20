"""Tests for the profile/workspace/jobs config grammar."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from pynchy.config.jobs import JobConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.config.settings import validate_settings_mapping

if TYPE_CHECKING:
    from pynchy.config import Settings


def _settings(**overrides) -> Settings:
    defaults = {
        "profiles": {"admin": ProfileConfig(is_admin=True)},
        "workspaces": {"admin": WorkspaceConfig(profiles=["admin"])},
    }
    defaults.update(overrides)
    return validate_settings_mapping(defaults)


def test_settings_use_profiles_and_workspaces_as_public_config_names() -> None:
    settings = _settings(
        profiles={
            "admin": ProfileConfig(
                prompts=["base"],
                skills=["pynchy", "browser"],
                tools=["shell"],
                repo="crypdick/pynchy",
                is_admin=True,
                contains_secrets=True,
                model="chatgpt/gpt-5.3-codex-spark",
            )
        },
        tools={"shell": {"type": "builtin", "name": "shell", "public_source": False}},
    )

    assert settings.profiles["admin"].contains_secrets is True
    assert settings.profiles["admin"].repo == ["crypdick/pynchy"]
    assert settings.workspaces["admin"].profiles == ["admin"]


def test_legacy_sandbox_sections_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Legacy config sections"):
        validate_settings_mapping(
            {
                "profiles": {"admin": ProfileConfig(is_admin=True)},
                "sandbox": {"admin": {"profiles": ["admin"]}},
            }
        )


def test_host_is_reserved_and_cannot_be_a_workspace_name() -> None:
    with pytest.raises(ValidationError, match="'host' is reserved"):
        _settings(workspaces={"host": WorkspaceConfig(profiles=["admin"])})


def test_workspace_profile_reference_must_exist() -> None:
    with pytest.raises(
        ValidationError, match=r"workspaces\.admin\.profiles references unknown profile"
    ):
        _settings(
            profiles={},
            workspaces={"admin": WorkspaceConfig(profiles=["missing"])},
        )


def test_workspace_migration_must_target_a_declared_child_thread() -> None:
    with pytest.raises(ValidationError, match="targets undeclared thread"):
        validate_settings_mapping(
            {
                "workspaces": {
                    "relationships": {"threads": [{"name": "family"}]},
                },
                "workspace_migrations": {
                    "fam": {
                        "target_workspace": "relationships",
                        "target_thread": "other",
                    }
                },
            }
        )


def test_workspace_migration_retirement_requires_all_retargeting_confirmations() -> None:
    with pytest.raises(ValidationError, match="retire_legacy_workspace requires"):
        validate_settings_mapping(
            {
                "workspaces": {
                    "relationships": {"threads": [{"name": "family"}]},
                },
                "workspace_migrations": {
                    "fam": {
                        "target_workspace": "relationships",
                        "target_thread": "family",
                        "retire_legacy_workspace": True,
                    }
                },
            }
        )


@pytest.mark.parametrize(
    ("config", "configured_path"),
    [
        (
            {"agent": {"default_core": "claude", "model": "custom-model"}},
            "agent.model",
        ),
        (
            {
                "agent": {"default_core": "claude"},
                "profiles": {"unused": {"model": "custom-model"}},
            },
            "profiles.unused.model",
        ),
        (
            {
                "agent": {"default_core": "claude"},
                "workspaces": {"admin": {"model": "custom-model"}},
            },
            "workspaces.admin.model",
        ),
    ],
)
def test_claude_sdk_rejects_model_overrides(config, configured_path: str) -> None:
    with pytest.raises(
        ValidationError, match="currently hard-codes its model to 'opus'"
    ) as exc_info:
        validate_settings_mapping(config)

    assert configured_path in str(exc_info.value)


def test_claude_cli_accepts_workspace_model_override() -> None:
    settings = validate_settings_mapping(
        {
            "agent": {"default_core": "claude-cli", "model": "global-model"},
            "profiles": {"base": {"model": "profile-model"}},
            "workspaces": {
                "admin": {
                    "profiles": ["base"],
                    "model": "workspace-model",
                }
            },
        }
    )

    resolved = settings.resolved_workspace_config("admin")

    assert resolved is not None
    assert resolved.model == "workspace-model"


def test_agent_job_targets_configured_workspace() -> None:
    settings = _settings(
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


def test_agent_job_workspace_can_use_a_profile_shared_by_other_roots() -> None:
    settings = _settings(
        workspaces={
            "relationships": WorkspaceConfig(profiles=["admin"]),
            "relationships-archive": WorkspaceConfig(profiles=["admin"]),
        },
        jobs={
            "fam_daily_checkin": JobConfig(
                enabled=True,
                schedule="0 8 * * *",
                workspace="relationships",
                prompt="Check in.",
            )
        },
    )

    assert settings.jobs["fam_daily_checkin"].workspace == "relationships"


def test_workspace_scopes_keep_policy_separate_from_discord_parent() -> None:
    settings = _settings(
        profiles={
            "relationships": ProfileConfig(),
            "fam": ProfileConfig(repo="crypdick/fam"),
            "pynchy-dev": ProfileConfig(
                repo="crypdick/pynchy",
                execution_mode="host",
                cwd="/srv/pynchy",
                is_admin=True,
            ),
        },
        workspaces={
            "relationships": {
                "profiles": ["relationships"],
                "scopes": [{"workspace": "fam", "profiles": ["fam"]}],
            },
            "admin": {
                "profiles": ["relationships"],
                "scopes": [{"workspace": "pynchy-dev", "profiles": ["pynchy-dev"]}],
            },
        },
        jobs={
            "fam-check": {
                "schedule": "0 8 * * *",
                "workspace": "fam",
                "prompt": "Check the family board.",
            },
            "pynchy-check": {
                "schedule": "0 9 * * *",
                "workspace": "pynchy-dev",
                "prompt": "Check the Pynchy board.",
            },
        },
    )

    fam = settings.resolved_workspace_config("fam")
    pynchy_dev = settings.resolved_workspace_config("pynchy-dev")

    assert settings.workspace_parent("fam") == "relationships"
    assert settings.workspace_parent("pynchy-dev") == "admin"
    assert fam is not None
    assert fam.repo == ["crypdick/fam"]
    assert fam.execution_mode == "container"
    assert pynchy_dev is not None
    assert pynchy_dev.repo == ["crypdick/pynchy"]
    assert pynchy_dev.execution_mode == "host"
    assert pynchy_dev.is_admin is True


def test_deterministic_workspace_job_supports_interval_schedule() -> None:
    job = JobConfig(
        enabled=True,
        interval_minutes=360,
        workspace="admin",
        agent=False,
        command="scripts/check.sh",
    )

    assert job.is_deterministic is True
    assert job.interval_minutes == 360


def test_agent_job_requires_workspace_selector() -> None:
    with pytest.raises(ValidationError, match="agent jobs require workspace"):
        JobConfig(
            enabled=True,
            schedule="0 8 * * *",
            prompt="Check in.",
        )


def test_agent_job_workspace_must_exist() -> None:
    with pytest.raises(ValidationError, match="references unknown workspace"):
        _settings(
            jobs={
                "daily-triage": JobConfig(
                    enabled=True,
                    schedule="0 8 * * *",
                    workspace="missing",
                    prompt="Check in.",
                )
            }
        )


def test_one_time_agent_job_uses_at_instead_of_schedule() -> None:
    job = JobConfig(
        enabled=True,
        at="2026-07-08T18:30:00-07:00",
        workspace="admin",
        prompt="Cancel the subscription.",
    )

    assert job.at == "2026-07-08T18:30:00-07:00"
    assert job.schedule is None


def test_one_time_agent_job_rejects_invalid_at_timestamp() -> None:
    with pytest.raises(ValidationError, match="job at must be an ISO datetime"):
        JobConfig(
            enabled=True,
            at="tomorrow-ish",
            workspace="admin",
            prompt="Cancel the subscription.",
        )


def test_profile_capability_rules_resolve_into_workspace_policy() -> None:
    settings = _settings(
        profiles={
            "worker": {
                "tools": ["email"],
                "capabilities": {
                    "mcp.email.send": {"decision": "deny"},
                    "mcp.email.preview": {"decision": "allow"},
                },
            }
        },
        workspaces={"admin": WorkspaceConfig(profiles=["worker"])},
        tools={"email": {"type": "builtin"}},
    )

    resolved = settings.resolved_workspace_config("admin")

    assert resolved is not None
    assert resolved.capabilities["mcp.email.send"].decision == "deny"
    assert resolved.capabilities["mcp.email.preview"].decision == "allow"


def test_profile_execution_mode_and_cwd_resolve_for_workspace() -> None:
    settings = _settings(
        profiles={
            "base": ProfileConfig(cwd="/opt/pynchy-base"),
            "host": ProfileConfig(
                includes=["base"],
                is_admin=True,
                execution_mode="host",
                cwd="/opt/pynchy-project",
            ),
        },
        workspaces={"admin": WorkspaceConfig(profiles=["host"])},
    )

    resolved = settings.resolved_workspace_config("admin")

    assert resolved is not None
    assert resolved.execution_mode == "host"
    assert resolved.cwd == "/opt/pynchy-project"


def test_profile_skill_denials_resolve_for_workspace() -> None:
    settings = _settings(
        profiles={
            "base": ProfileConfig(denied_skills=["legacy-skill"]),
            "worker": ProfileConfig(
                includes=["base"],
                denied_skills=["expensive-skill", "legacy-skill"],
            ),
        },
        workspaces={"admin": WorkspaceConfig(profiles=["worker"])},
    )

    resolved = settings.resolved_workspace_config("admin")

    assert resolved is not None
    assert resolved.denied_skills == ["legacy-skill", "expensive-skill"]


def test_host_execution_mode_requires_admin_workspace() -> None:
    with pytest.raises(ValidationError, match="execution_mode = 'host' requires is_admin"):
        _settings(
            profiles={
                "host": ProfileConfig(
                    execution_mode="host",
                    cwd="/opt/pynchy-project",
                )
            },
            workspaces={"admin": WorkspaceConfig(profiles=["host"])},
        )


def test_host_execution_mode_requires_cwd() -> None:
    with pytest.raises(ValidationError, match="execution_mode = 'host' requires cwd"):
        _settings(
            profiles={
                "host": ProfileConfig(
                    is_admin=True,
                    execution_mode="host",
                )
            },
            workspaces={"admin": WorkspaceConfig(profiles=["host"])},
        )


def test_job_requires_exactly_one_schedule_shape() -> None:
    with pytest.raises(ValidationError, match="exactly one of schedule, interval_minutes, or at"):
        JobConfig(
            enabled=True,
            schedule="0 8 * * *",
            at="2026-07-08T18:30:00-07:00",
            workspace="admin",
            prompt="Nope.",
        )


def test_host_job_is_selected_by_workspace_magic_word() -> None:
    settings = _settings(
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
