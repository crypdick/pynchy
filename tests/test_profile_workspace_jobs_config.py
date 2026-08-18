"""Tests for the profile/workspace/jobs config grammar."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from pynchy.config.api import (
    BuiltinTool,
    CanaryConfig,
    ChannelOverrideConfig,
    DiscordConnectionConfig,
    JobConfig,
    MatrixConnectionConfig,
    McpTool,
    McpToolConfig,
    PermissionConfig,
    ProfileConfig,
    RepoConfig,
    ReposConfig,
    SchedulerConfig,
    Settings,
    WorkspaceConfig,
    WorkspaceTool,
    get_settings,
    reset_settings,
    validate_settings_mapping,
)

DISCORD_BOT_ENV = "X"


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


def test_profile_repository_none_normalizes_to_an_empty_list() -> None:
    profile = ProfileConfig(repo=None)

    assert profile.repo == []


def test_permission_config_lazy_export_remains_available() -> None:
    assert PermissionConfig.__name__ == "PermissionConfig"


def test_repo_config_resolves_relative_paths_and_accepts_default_path() -> None:
    assert RepoConfig().path is None
    assert RepoConfig.model_validate({"path": None}).path is None
    assert RepoConfig(path="relative/repo").path.endswith("/relative/repo")


def test_discord_runtime_settings_preserve_security_allowlist() -> None:
    runtime = DiscordConnectionConfig(
        bot_token_env=DISCORD_BOT_ENV,
        security=ChannelOverrideConfig(allowed_users=["user"]),
    ).to_runtime_settings()

    assert runtime.security is not None
    assert runtime.security.allowed_users == ["user"]


def test_mcp_tool_lookup_returns_mcp_configs_and_rejects_unknown_tools() -> None:
    settings = _settings(
        tools={
            "reader": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="script", command="reader", port=8475),
            ),
            "shell": BuiltinTool(type="builtin"),
        }
    )

    assert set(settings.mcp_tools_for_names(["reader", "shell"])) == {"reader"}
    with pytest.raises(ValueError, match="unknown tool: missing"):
        settings.mcp_tools_for_names(["missing"])


def test_admin_clean_room_allows_workspace_tools() -> None:
    settings = _settings(
        profiles={"admin": ProfileConfig(is_admin=True, tools=["workspace"])},
        tools={"workspace": WorkspaceTool(type="workspace")},
    )

    assert settings.workspace_config("admin") is not None


def test_semantic_child_without_static_workspace_is_skipped() -> None:
    settings = _settings(
        workspaces={
            "admin": WorkspaceConfig(
                profiles=["admin"],
                scopes=[{"workspace": "child", "profiles": ["admin"]}],
            )
        }
    )

    assert "child" in settings.workspace_names()


def test_admin_clean_room_fails_closed_when_resolution_disappears() -> None:
    resolved = Mock(is_admin=True, tools=(), execution_mode="container")
    with patch.object(Settings, "resolved_workspace_config", side_effect=[None, resolved, None]):
        settings = _settings()

    assert settings.workspace_config("admin") is not None


def test_admin_clean_room_skips_a_missing_workspace_policy() -> None:
    with patch.object(Settings, "workspace_config", return_value=None):
        settings = _settings()

    assert settings.workspace_names() == ("admin",)


def test_timezone_uses_configured_value() -> None:
    assert _settings(scheduler=SchedulerConfig(timezone="UTC")).timezone == "UTC"


def test_get_settings_initializes_the_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings()
    sentinel = Mock(spec=Settings)
    monkeypatch.setattr("pynchy.config.settings.Settings", lambda: sentinel)

    assert get_settings() is sentinel
    assert get_settings() is sentinel


def test_workspace_pipeline_rejects_blank_text() -> None:
    with pytest.raises(ValidationError, match="workspace pipeline cannot be empty"):
        WorkspaceConfig(pipeline="  ")


def test_repos_config_rejects_inline_overrides_and_non_mapping_input() -> None:
    with pytest.raises(ValidationError, match=r"nested under repos\.overrides"):
        ReposConfig.model_validate({"legacy": {}})
    with pytest.raises(ValidationError):
        ReposConfig.model_validate([])


@pytest.mark.parametrize(
    "interval",
    [
        {"git_sync_interval_seconds": 0},
        {"channel_reconciliation_interval_seconds": -1},
    ],
)
def test_scheduler_rejects_non_positive_polling_intervals(interval: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="scheduler intervals must be positive"):
        SchedulerConfig(**interval)


def test_enabled_canary_requires_a_target_profile() -> None:
    with pytest.raises(ValidationError, match="target_profile is required"):
        CanaryConfig(enabled=True, target_profile=" ")


def test_canary_rejects_an_invalid_schedule() -> None:
    with pytest.raises(ValidationError, match="Invalid cron expression"):
        CanaryConfig(schedule="not a cron expression")


@pytest.mark.parametrize(
    "name",
    ["../secret", "nested/prompt", "souls/Uppercase", "souls/has space"],
)
def test_workspace_soul_ids_must_be_scoped_safe_identifiers(name: str) -> None:
    with pytest.raises(ValidationError, match="prompt IDs"):
        WorkspaceConfig(soul=name)


def test_workspace_soul_must_use_the_souls_scope() -> None:
    with pytest.raises(ValidationError, match="workspace soul must use the souls/ scope"):
        WorkspaceConfig(soul="executors/default")


def test_matrix_gateway_environment_name_must_be_valid() -> None:
    config = MatrixConnectionConfig(
        expected_user_id="@owner:example.test",
        gateway_command_env="MATRIX_GATEWAY",
    )
    assert config.gateway_command_env == "MATRIX_GATEWAY"
    with pytest.raises(ValidationError, match="gateway_command_env"):
        MatrixConnectionConfig(
            expected_user_id="@owner:example.test",
            gateway_command_env="not-an-environment-name",
        )


def test_legacy_sandbox_sections_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown config sections"):
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


def test_claude_sdk_without_model_overrides_is_valid() -> None:
    settings = validate_settings_mapping({"agent": {"default_core": "claude"}})

    assert settings.agent.default_core == "claude"


def test_agent_job_targets_configured_workspace() -> None:
    settings = _settings(
        jobs={
            "daily-triage": JobConfig(
                enabled=True,
                schedule="0 8 * * *",
                workspace="admin",
                prompt="Triage today's work.",
            )
        }
    )

    job = settings.jobs["daily-triage"]
    assert job.workspace == "admin"
    assert job.schedule == "0 8 * * *"
    assert job.prompt == "Triage today's work."
    assert job.memory is True


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
                "scopes": [
                    {
                        "workspace": "pynchy-dev",
                        "profiles": ["pynchy-dev"],
                        "model_reasoning_effort": "medium",
                    }
                ],
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

    resolved = settings.resolved_workspace_config("pynchy-dev")
    assert resolved is not None
    assert resolved.model_reasoning_effort == "medium"

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


def test_profile_permission_rules_resolve_into_workspace_policy() -> None:
    settings = _settings(
        profiles={
            "worker": {
                "tools": ["email"],
                "permissions": {
                    "deny": ["mcp.email.send"],
                    "allow": ["mcp.email.preview"],
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


def test_agent_job_requires_prompt() -> None:
    with pytest.raises(ValidationError, match="agent jobs require prompt"):
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schedule", "not cron", "Invalid cron expression"),
        ("workspace", "   ", "job workspace cannot be empty"),
        ("command", "   ", "host job command cannot be empty"),
        ("timeout_seconds", 0, "timeout_seconds must be positive"),
        ("display_name", "   ", "job text fields cannot be empty"),
    ],
)
def test_job_rejects_invalid_field_values(field: str, value: str | int, message: str) -> None:
    values: dict[str, str | int] = {
        "schedule": "0 8 * * *",
        "workspace": "host",
        "command": "scripts/backup.sh",
    }
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        JobConfig(**values)


def test_host_job_requires_command_and_rejects_pre_run_fields() -> None:
    with pytest.raises(ValidationError, match="host jobs require command"):
        JobConfig(schedule="0 8 * * *", workspace="host")

    with pytest.raises(ValidationError, match="host jobs cannot set pre-run fields"):
        JobConfig(
            schedule="0 8 * * *",
            workspace="host",
            command="scripts/backup.sh",
            pre_run_command="scripts/prepare.sh",
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"agent": False},
            "deterministic workspace jobs require command",
        ),
        (
            {"agent": False, "command": "scripts/check.sh", "prompt": "Nope"},
            "deterministic workspace jobs cannot set prompts",
        ),
        (
            {"agent": False, "command": "scripts/check.sh", "pre_run_cwd": "project"},
            "deterministic workspace jobs cannot set pre-run fields",
        ),
        (
            {"command": "scripts/check.sh", "prompt": "Nope"},
            "agent jobs cannot set command",
        ),
        (
            {"prompt": "Nope", "pre_run_cwd": "project"},
            "agent job pre-run options require pre_run_command",
        ),
    ],
)
def test_workspace_job_rejects_an_incompatible_execution_shape(
    values: dict[str, str | bool],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        JobConfig(schedule="0 8 * * *", workspace="admin", **values)
